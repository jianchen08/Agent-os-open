#!/usr/bin/env python3
"""HowToLiveBetter（高性价比人生指南）→ AgentOS 知识库 同步脚本。

把上游 github.com/eternity4719/HowToLiveBetter 的 34 章 book/*.md 逐章经
KB 上传接口（hindsight 原生摄取管线，verbatim 主模式 + kb_chunks 降级）入库。
设计真值 = docs/working/2026-11-19-KB原生摄取管线改造_ADR草稿.md（决策 6）
+ 卡1 交付说明 docs/working/{{task_id}}_kb_native_ingestion_交付说明.md。

流程：
  1. 获取上游：--repo-dir 指定既有克隆；否则浅克隆到 --work-dir（缺省
     .zctmp/sync_life_guide/HowToLiveBetter）。克隆失败如实报错退出（禁静默）。
  2. 登录目标内核：POST /api/v1/auth/login（username=admin）换 Bearer。
     口令来源（顺序固定，.env 是真值面优先于环境变量历史快照——
     scripts/health_probe.py 同序同依据）：--password > 用户空间
     %APPDATA%/agentos/.env > 仓根 .env > AGENTOS_ADMIN_PASSWORD。
  3. 幂等（先删后传）：GET knowledge-base 列表按 name（=章节文件名）匹配
     既有条目 → DELETE /knowledge-base/{item_id} → 再上传。item_id 由服务端
     随机生成（knowledge_base.upload_document uuid4），无法按章节名稳定派生，
     故选「先删后传」而非更新语义（删除路由按 type:knowledge + kb_item:{id}
     标签双 bank 覆盖，卡1 实现）。
  4. 逐章 POST /ext/hindsight_memory_service/knowledge-base/upload
     （multipart file 字段），响应含 ingestion_mode/bank_id/degraded/
     degradation_reason（降级面如实标注，禁静默）。
  5. 报告落盘 markdown：成功清单（含 ingestion_mode）/降级清单（含原因）/
     失败清单/先删后传清单。

用法：
    python scripts/sync_life_guide.py --dry-run          # 只盘点不写
    python scripts/sync_life_guide.py                    # 全量 34 章
    python scripts/sync_life_guide.py --only 01,02       # 指定章节
    python scripts/sync_life_guide.py --base-url http://127.0.0.1:18230 \\
        --password <scratch 实例口令>                     # scratch 实例验证

退出码：0 = 全部章节成功（含降级）；1 = 有失败/环境不可用。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM_URL = "https://github.com/eternity4719/HowToLiveBetter.git"
DEFAULT_WORK_DIR = ROOT / ".zctmp" / "sync_life_guide" / "HowToLiveBetter"
DEFAULT_BASE_URL = "http://127.0.0.1:9100"
EXPECTED_CHAPTERS = 34
KB_BASE = "/ext/hindsight_memory_service/knowledge-base"
REQUEST_TIMEOUT_S = 60


# ── 口令面（.env 真值面优先；错误口令触发内核断连非 401，禁历史值）──────────


def _read_env_password(env_path: Path) -> str | None:
    """从 .env 文件读 AGENTOS_ADMIN_PASSWORD（缺文件/缺键 = None）。"""
    try:
        lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for raw in lines:
        if raw.startswith("AGENTOS_ADMIN_PASSWORD="):
            value = raw.split("=", 1)[1].strip()
            if value:
                return value
    return None


def resolve_password(explicit: str | None) -> tuple[str | None, str]:
    """口令解析（顺序固定）：--password > 用户空间 .env > 仓根 .env > 环境变量。

    返回 (口令, 来源说明)；都缺 = (None, 来源说明)。
    """
    if explicit and explicit.strip():
        return explicit.strip(), "--password 参数"
    appdata = os.environ.get("APPDATA")
    if appdata:
        pw = _read_env_password(Path(appdata) / "agentos" / ".env")
        if pw:
            return pw, "用户空间 .env（%APPDATA%/agentos/.env）"
    pw = _read_env_password(ROOT / ".env")
    if pw:
        return pw, "仓根 .env"
    env_pw = os.environ.get("AGENTOS_ADMIN_PASSWORD")
    if env_pw and env_pw.strip():
        return env_pw.strip(), "环境变量 AGENTOS_ADMIN_PASSWORD（历史快照，可能陈旧）"
    return None, "未找到（--password / .env / 环境变量均无）"


# ── 上游获取 ────────────────────────────────────────────────────────────────


def ensure_upstream(repo_dir: Path, work_dir: Path) -> tuple[Path, str]:
    """上游克隆就位：--repo-dir 优先，否则浅克隆到 work_dir。

    返回 (book 目录, 上游描述)。克隆失败如实抛错（禁静默跳过）。
    """
    if repo_dir is not None:
        book = repo_dir / "book"
        if not book.is_dir():
            raise SystemExit(f"[sync] --repo-dir 下无 book/ 目录: {repo_dir}")
        return book, f"既有克隆 {repo_dir}"
    if not (work_dir / ".git").is_dir():
        work_dir.mkdir(parents=True, exist_ok=True)
        print(f"[sync] 浅克隆上游 → {work_dir} ...")
        proc = subprocess.run(  # noqa: S603 — 固定参数列表，无 shell
            ["git", "clone", "--depth", "1", UPSTREAM_URL, str(work_dir)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(
                f"[sync] 上游克隆失败（网络不可达/仓库不存在，如实报错不静默）:"
                f" git exit {proc.returncode}\n{proc.stderr.strip()}"
            )
    book = work_dir / "book"
    if not book.is_dir():
        raise SystemExit(f"[sync] 克隆完成但 book/ 缺失: {work_dir}")
    return book, f"浅克隆 {work_dir}"


def list_chapters(book_dir: Path, only: list[str] | None) -> list[Path]:
    """book/*.md 章节清单（文件名排序）；--only 按文件名前缀过滤。"""
    chapters = sorted(p for p in book_dir.glob("*.md") if p.is_file())
    if only:
        chapters = [p for p in chapters if any(p.name.startswith(o) for o in only)]
    return chapters


# ── 内核 REST（登录换 Bearer + KB 端点）────────────────────────────────────


def _http(
    url: str,
    *,
    method: str = "GET",
    token: str | None = None,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = REQUEST_TIMEOUT_S,
) -> tuple[int, dict[str, Any]]:
    """一次 HTTP 请求 → (status, json dict)；非 JSON 响应体落 {"raw": ...}。"""
    req_headers = dict(headers or {})
    if token:
        req_headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        body = exc.read()
        status = exc.code
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"请求失败 {method} {url}: {exc}") from exc
    try:
        return status, json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, {"raw": body[:500].decode("utf-8", errors="replace")}


def login(base_url: str, password: str, username: str) -> str:
    """登录换 access_token；失败如实抛错（口令面故障不静默）。"""
    status, payload = _http(
        f"{base_url}/api/v1/auth/login",
        method="POST",
        data=json.dumps({"username": username, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    token = payload.get("access_token") if isinstance(payload, dict) else None
    if status != 200 or not token:
        raise SystemExit(
            f"[sync] 登录失败: HTTP {status}（口令不匹配或用户不存在；口令面检查 --password 与 .env 真值面）"
        )
    return str(token)


def multipart_body(filename: str, content: bytes, mime: str) -> tuple[bytes, str]:
    """multipart/form-data 构造（file 字段；与 http_json.parse_multipart 对齐）。"""
    boundary = f"hltb{uuid.uuid4().hex}"
    part = (
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {mime}\r\n\r\n"
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return part, f"multipart/form-data; boundary={boundary}"


# ── 同步主流程 ──────────────────────────────────────────────────────────────


def fetch_existing_items(base_url: str, token: str) -> dict[str, dict[str, Any]]:
    """KB 列表 → {name: item}（同名取最新一条；先删后传的匹配面）。"""
    status, payload = _http(f"{base_url}{KB_BASE}", token=token)
    if status != 200 or not isinstance(payload, list):
        raise SystemExit(f"[sync] KB 列表拉取失败: HTTP {status} {payload}")
    by_name: dict[str, dict[str, Any]] = {}
    for item in payload:
        name = str(item.get("name") or item.get("filename") or "")
        if name:
            by_name[name] = item
    return by_name


def delete_item(base_url: str, token: str, item_id: str) -> tuple[bool, str]:
    """删除 KB 条目（幂等：404 视为已删）。"""
    status, payload = _http(f"{base_url}{KB_BASE}/{item_id}", method="DELETE", token=token)
    if status in (200, 404):
        return True, ""
    return False, f"HTTP {status}: {json.dumps(payload, ensure_ascii=False)[:200]}"


def upload_chapter(base_url: str, token: str, chapter: Path) -> tuple[bool, dict[str, Any]]:
    """单章上传；返回 (成功?, 响应/错误 dict)。降级（degraded=true）算成功。"""
    content = chapter.read_bytes()
    body, content_type = multipart_body(chapter.name, content, "text/markdown")
    status, payload = _http(
        f"{base_url}{KB_BASE}/upload",
        method="POST",
        token=token,
        data=body,
        headers={"Content-Type": content_type},
    )
    ok = status == 200 and isinstance(payload, dict) and "error" not in payload
    return ok, {"http": status, **payload}


def write_report(
    report_path: Path,
    upstream_desc: str,
    base_url: str,
    chapters: list[Path],
    uploaded: list[dict[str, Any]],
    degraded: list[dict[str, Any]],
    failed: list[dict[str, Any]],
    deleted: list[dict[str, Any]],
) -> None:
    """同步报告落盘（成功/降级含 ingestion_mode 标注/失败/先删后传 四节）。"""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# HowToLiveBetter → 知识库 同步报告",
        "",
        f"- 时间：{now}",
        f"- 上游：{UPSTREAM_URL}（{upstream_desc}）",
        f"- 目标内核：{base_url}",
        f"- 章节面：book/ 实见 {len(chapters)} 章（全书应为 {EXPECTED_CHAPTERS} 章）",
        f"- 结果：成功 {len(uploaded)} / 降级 {len(degraded)} / 失败 {len(failed)} / 先删后传 {len(deleted)}",
        "",
    ]
    lines += ["## 成功清单", ""]
    if uploaded:
        lines += [
            "| 章节 | item_id | ingestion_mode | bank | 大小 |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {r['chapter']} | {r.get('item_id', '')} | {r.get('ingestion_mode', '')}"
            f" | {r.get('bank_id', '')} | {r.get('size', '')} |"
            for r in uploaded
        ]
    else:
        lines += ["（无）"]
    lines += [
        "",
        "## 降级清单（degraded=true，ingestion_mode=chunks——verbatim 不可用，如实标注禁静默）",
        "",
    ]
    if degraded:
        lines += [
            "| 章节 | item_id | ingestion_mode | bank | degradation_reason |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| {r['chapter']} | {r.get('item_id', '')} | {r.get('ingestion_mode', '')}"
            f" | {r.get('bank_id', '')} | {r.get('degradation_reason', '')} |"
            for r in degraded
        ]
    else:
        lines += ["（无）"]
    lines += ["", "## 失败清单", ""]
    if failed:
        lines += ["| 章节 | HTTP | 错误 |", "|---|---|---|"]
        lines += [
            f"| {r['chapter']} | {r.get('http', '')} | {r.get('error', json.dumps(r, ensure_ascii=False)[:200])} |"
            for r in failed
        ]
    else:
        lines += ["（无）"]
    lines += ["", "## 先删后传（幂等删除面）", ""]
    if deleted:
        lines += ["| 章节 | 已删 item_id |", "|---|---|"]
        lines += [f"| {r['chapter']} | {r.get('deleted_id', '')} |" for r in deleted]
    else:
        lines += ["（无——首次同步或同名条目不存在）"]
    lines += ["", "---", "", "报告生成：scripts/sync_life_guide.py（幂等 = 先删后传，按章节文件名匹配）", ""]
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HowToLiveBetter 34 章 → AgentOS 知识库同步（hindsight 原生管线）")
    parser.add_argument("--repo-dir", type=Path, default=None, help="既有上游克隆根（含 book/）")
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR, help="浅克隆目标目录")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="内核基址（默认 dev 9100）")
    parser.add_argument("--username", default="admin", help="登录用户名（默认 admin）")
    parser.add_argument("--password", default=None, help="登录口令（缺省按 .env 优先链解析）")
    parser.add_argument("--only", default=None, help="只同步指定章节（逗号分隔文件名前缀，如 01,02）")
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="报告路径（缺省 reports/life_guide_sync_报告_<时间戳>.md）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只盘点章节与登录，不写库")
    args = parser.parse_args(argv)

    book_dir, upstream_desc = ensure_upstream(args.repo_dir, args.work_dir)
    only = [s.strip() for s in args.only.split(",")] if args.only else None
    chapters = list_chapters(book_dir, only)
    if len(list(book_dir.glob("*.md"))) != EXPECTED_CHAPTERS:
        print(
            f"[sync][warn] 上游 book/ 章节数 {len(list(book_dir.glob('*.md')))}"
            f" != 预期 {EXPECTED_CHAPTERS}（上游可能增删章节，如实继续）"
        )
    if not chapters:
        raise SystemExit("[sync] 无可同步章节（--only 过滤后为空或 book/ 为空）")
    print(f"[sync] 上游就绪：{upstream_desc}；本轮同步 {len(chapters)} 章")

    password, pw_source = resolve_password(args.password)
    if not password:
        raise SystemExit(
            f"[sync] 登录口令缺失（{pw_source}）——口令恒读 .env（AGENTS.md 铁律），"
            "或用 --password 显式传入（scratch 实例验证场景）"
        )
    token = login(args.base_url, password, args.username)
    print(f"[sync] 登录成功（口令来源：{pw_source}）")

    if args.dry_run:
        for ch in chapters:
            print(f"  [dry-run] 将同步 {ch.name}（{ch.stat().st_size} bytes）")
        print("[sync] dry-run 完成，未写库；报告不生成")
        return 0

    existing = fetch_existing_items(args.base_url, token)
    uploaded: list[dict[str, Any]] = []
    degraded: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []

    for chapter in chapters:
        prev = existing.get(chapter.name)
        if prev:
            item_id = str(prev.get("id") or prev.get("item_id") or "")
            ok, err = delete_item(args.base_url, token, item_id)
            if ok:
                deleted.append({"chapter": chapter.name, "deleted_id": item_id})
                print(f"  [sync] 已删旧条目 {chapter.name} (item_id={item_id})")
            else:
                failed.append({"chapter": chapter.name, "error": f"删除失败: {err}"})
                print(f"  [sync][fail] {chapter.name} 删除失败：{err}")
                continue
        ok, payload = upload_chapter(args.base_url, token, chapter)
        record = {"chapter": chapter.name, **payload}
        if ok:
            if payload.get("degraded"):
                degraded.append(record)
                print(
                    f"  [sync][degraded] {chapter.name} → {payload.get('bank_id')}"
                    f"（{payload.get('degradation_reason', '')}）"
                )
            else:
                uploaded.append(record)
                print(
                    f"  [sync] {chapter.name} → item_id={payload.get('item_id')}"
                    f" mode={payload.get('ingestion_mode')} bank={payload.get('bank_id')}"
                )
        else:
            failed.append(record)
            print(f"  [sync][fail] {chapter.name}: HTTP {payload.get('http')} {payload}")

    report_path = args.report or (
        ROOT / "reports" / ("life_guide_sync_报告_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".md")
    )
    write_report(
        report_path,
        upstream_desc,
        args.base_url,
        chapters,
        uploaded,
        degraded,
        failed,
        deleted,
    )
    print(
        f"[sync] 完成：成功 {len(uploaded)} / 降级 {len(degraded)} / 失败 {len(failed)}"
        f" / 先删后传 {len(deleted)}；报告 → {report_path}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
