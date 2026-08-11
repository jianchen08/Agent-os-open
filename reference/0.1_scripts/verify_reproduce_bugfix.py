#!/usr/bin/env python3
"""前端 4 Bug 修复功能验证 — 可复现验证脚本。

覆盖场景（对应 docs/working/frontend_ui_bugfix_function_verify_report.md）：

  Bug2 404 根因（三个新端点实际可用，200 而非 404/500）：
    E1 GET /ext/channel_api/search?q=测试&type=all&limit=20 -> 200
       返回 {query, type, sessions, messages} 结构
    E2 GET /ext/channel_api/search?q=（空）-> 200 空结果
    E3 GET /ext/channel_api/files/capabilities?model_name=xxx -> 200
       返回模型文件能力字段（v1 Must Fix：_handle_files_domain async def 生效）
    E4 GET /ext/channel_api/files/supported-types -> 200
       返回 image_types/document_types/max 字段
    E5 对照：旧路径 /api/v1/search -> 404（证明前端路径迁移的必要性）
    E6 search POST 方法 -> 404（路由仅声明 GET，符合预期）
    E7 未知域 -> 404（对照，未迁移域明确 404 而非 500）

  Bug1/3/4 静态扫描（代码级）：
    S1 frontend/src 无 /api/v1/workspaces、/api/v1/search、/api/v1/tasks/debug、
       /api/v1/files/* 旧路径残留
    S2 Sidebar.tsx 无 searchType state；SessionSearch.tsx 无 TYPE_OPTIONS/Tab
    S3 Sidebar.tsx 无 navigate('/session/')；sessionListStore.ts 无
       bumpWorkspaceDataVersion 调用（setActiveSession 仅保留 loadPipelineMessages）
    S4 agentStore.fetchAgents 按 config_id 去重；SessionEditModal option key
       追加 index 唯一化

用法：
    python3 verify_reproduce_bugfix.py

前置依赖：
    pip install fastapi pydantic-settings bcrypt PyJWT
    （可选）frontend/ 下 npx tsc --noEmit 检查前端类型

预期输出：全部 PASS。
[来源: docs/working/frontend_ui_bugfix_function_verify_report.md]
"""
# 注意：本脚本不启用 `from __future__ import annotations`，
# 否则 fastapi 会把 `request: Request` 注解解析为字符串，
# 无法识别为注入对象，导致 422 Field required。

import base64
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
CHANNEL_API_DIR = PROJECT_ROOT / "plugins" / "shared" / "system" / "channel_api"
SDK_SRC = PROJECT_ROOT / "plugins" / "sdk" / "src"

sys.path.insert(0, str(CHANNEL_API_DIR))
sys.path.insert(0, str(SDK_SRC))
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "src"))

_results: list[tuple[str, str, bool]] = []


def _record(scenario: str, detail: str, passed: bool) -> None:
    _results.append((scenario, detail, passed))
    status = "PASS" if passed else "FAIL"
    print(f"  [{status}] {scenario}: {detail}")
    if not passed:
        raise AssertionError(f"场景失败: {scenario} — {detail}")


def _make_client():
    """构造 FastAPI TestClient，把 /ext/channel_api/** 请求转发到 server.http_handle。

    模拟内核（axum :9100）的 /ext/{plugin_id}/** 通配路由转发行为：
    内核收到 /ext/channel_api/* 请求后调用 channel_api 插件 http.handle 分发。
    """
    import server
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.api_route(
        "/ext/channel_api/{path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    )
    async def ext_handler(path: str, request: Request):
        raw = await request.body()
        result = await server.http_handle(
            path=f"/ext/channel_api/{path}",
            method=request.method,
            raw_body=raw.decode("utf-8") if raw else "",
            headers=dict(request.headers),
            query=dict(request.query_params),
        )
        data = result.get("data", {})
        status = data.get("status", 200)
        body_b64 = data.get("body", "")
        body_json = (
            json.loads(base64.b64decode(body_b64).decode()) if body_b64 else {}
        )
        return JSONResponse(body_json, status_code=status)

    return TestClient(app)


# ---------------------------------------------------------------------------
# Bug2 端点运行时验证
# ---------------------------------------------------------------------------
def e1_search_ok() -> None:
    """E1: search 端点 200 + 结构验证。"""
    client = _make_client()
    resp = client.get(
        "/ext/channel_api/search",
        params={"q": "测试", "type": "all", "limit": "20"},
    )
    body = resp.json()
    ok = (
        resp.status_code == 200
        and body.get("query") == "测试"
        and body.get("type") == "all"
        and isinstance(body.get("sessions"), list)
        and isinstance(body.get("messages"), list)
    )
    _record("E1 search?q=测试&type=all&limit=20",
            f"status={resp.status_code} body={json.dumps(body, ensure_ascii=False)[:120]}",
            ok)


def e2_search_empty_q() -> None:
    """E2: search 空 q -> 200 空结果（搜索框清空场景）。"""
    client = _make_client()
    resp = client.get("/ext/channel_api/search", params={"q": "", "type": "all"})
    body = resp.json()
    ok = (
        resp.status_code == 200
        and body.get("sessions") == []
        and body.get("messages") == []
    )
    _record("E2 search 空q", f"status={resp.status_code} body={json.dumps(body, ensure_ascii=False)}", ok)


def e3_files_capabilities() -> None:
    """E3: files/capabilities -> 200 模型文件能力字段。

    审查报告 v1 Must Fix：_handle_files_domain 曾因同步/异步签名错误返回 500，
    已修复为 async def（server.py:1705）。本场景验证修复真实生效（200 而非 500）。
    """
    client = _make_client()
    resp = client.get(
        "/ext/channel_api/files/capabilities",
        params={"model_name": "glm-5.2"},
    )
    body = resp.json()
    expected = [
        "model_name", "supports_image", "supports_audio", "supports_video",
        "supported_image_types", "supported_audio_types", "supported_video_types",
        "max_image_size", "max_audio_size", "max_video_size", "is_multimodal",
    ]
    ok = resp.status_code == 200 and all(k in body for k in expected)
    _record("E3 files/capabilities?model_name=glm-5.2",
            f"status={resp.status_code} keys={list(body.keys())[:8]}",
            ok)


def e4_files_supported_types() -> None:
    """E4: files/supported-types -> 200 image_types/document_types/max。"""
    client = _make_client()
    resp = client.get("/ext/channel_api/files/supported-types")
    body = resp.json()
    ok = (
        resp.status_code == 200
        and "image_types" in body
        and "document_types" in body
        and "max_image_size" in body
        and "max_document_size" in body
    )
    _record("E4 files/supported-types",
            f"status={resp.status_code} keys={list(body.keys())}",
            ok)


def e5_old_path_404() -> None:
    """E5 对照: 旧路径 /api/v1/search -> 404（前端已迁移，不再请求）。"""
    client = _make_client()
    resp = client.get("/api/v1/search", params={"q": "x"})
    _record("E5 旧路径 /api/v1/search 对照",
            f"status={resp.status_code}（404=符合预期，证明前端迁移必要性）",
            resp.status_code == 404)


def e6_search_post_404() -> None:
    """E6: search POST 方法 -> 404（路由仅声明 GET）。"""
    client = _make_client()
    resp = client.post("/ext/channel_api/search", params={"q": "x"})
    _record("E6 search POST 方法",
            f"status={resp.status_code}（404=符合预期）",
            resp.status_code == 404)


def e7_unknown_domain_404() -> None:
    """E7: 未知域 -> 404（未迁移域明确 404 而非 500）。"""
    client = _make_client()
    resp = client.get("/ext/channel_api/not-exist-domain")
    _record("E7 未知域 404 对照",
            f"status={resp.status_code}（404=符合预期）",
            resp.status_code == 404)


# ---------------------------------------------------------------------------
# Bug1/3/4 静态扫描
# ---------------------------------------------------------------------------
def _grep(pattern: str, paths: list[str]) -> list[str]:
    # 使用 grep -E（ERE）确保 `|` 作为交替符而非字面量
    out = subprocess.run(
        ["grep", "-rnE", pattern, *paths],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
    )
    return out.stdout.strip().splitlines() if out.stdout.strip() else []


def s1_old_path_residue() -> None:
    """S1: frontend/src 无旧路径残留。"""
    hits = _grep(
        r"api/v1/workspaces|api/v1/search|api/v1/tasks/debug|api/v1/files/",
        ["frontend/src"],
    )
    # 排除 memory 的 /ext/channel_api/memory/search（P2 已迁移，非本次目标）
    hits = [h for h in hits if "api/v1/memory/search" not in h and "ext/channel_api/memory/search" not in h]
    _record("S1 frontend/src 旧路径残留",
            f"命中数={len(hits)}" + (f" 命中: {hits[:3]}" if hits else "（无残留）"),
            len(hits) == 0)


def s2_search_ui_unified() -> None:
    """S2: Bug1 搜索框统一 —— 无 searchType/TYPE_OPTIONS/Tab。"""
    hits = _grep(
        r"searchType|onSearchTypeChange|TYPE_OPTIONS",
        ["frontend/src/components/layout/Sidebar.tsx",
         "frontend/src/components/session/SessionSearch.tsx"],
    )
    _record("S2 搜索框无范围选择控件",
            f"命中数={len(hits)}" + (f" 命中: {hits[:3]}" if hits else "（无残留）"),
            len(hits) == 0)


def s3_no_session_navigate_no_bump() -> None:
    """S3: Bug4 切换会话局部刷新 —— Sidebar 无 /session/ navigate；
    sessionListStore.setActiveSession 无 bumpWorkspaceDataVersion 调用。"""
    nav_hits = _grep(r"navigate\('/session/|navigate\(`/session/",
                     ["frontend/src/components/layout/Sidebar.tsx"])
    bump_hits = _grep(r"bumpWorkspaceDataVersion\(",
                      ["frontend/src/stores/sessionListStore.ts"])
    ok = len(nav_hits) == 0 and len(bump_hits) == 0
    _record("S3 会话切换无 navigate/bump",
            f"navigate('/session/')命中={len(nav_hits)}, bumpWorkspaceDataVersion(调用)命中={len(bump_hits)}",
            ok)


def s4_agent_dedup_key_unique() -> None:
    """S4: Bug3 重复 key —— agentStore 去重 + SessionEditModal key 唯一化。"""
    dedup_hits = _grep(r"seenConfigIds|dedupedAgents",
                       ["frontend/src/stores/agentStore.ts"])
    key_hits = _grep(r"\$\{agent\.configId \|\| agent\.id\}-\$\{index\}",
                     ["frontend/src/components/session/SessionEditModal.tsx"])
    ok = len(dedup_hits) > 0 and len(key_hits) > 0
    _record("S4 agent 去重 + key 唯一化",
            f"agentStore 去重代码={len(dedup_hits)}处, SessionEditModal key唯一化={len(key_hits)}处",
            ok)


def main() -> int:
    print("=" * 70)
    print("前端 4 Bug 修复功能验证（可复现脚本）")
    print("=" * 70)

    scenarios = [
        e1_search_ok, e2_search_empty_q,
        e3_files_capabilities, e4_files_supported_types,
        e5_old_path_404, e6_search_post_404, e7_unknown_domain_404,
        s1_old_path_residue, s2_search_ui_unified,
        s3_no_session_navigate_no_bump, s4_agent_dedup_key_unique,
    ]

    for scenario in scenarios:
        try:
            scenario()
        except Exception as exc:
            _record(scenario.__name__, f"异常: {exc}", False)

    print("\n" + "=" * 70)
    total = len(_results)
    passed = sum(1 for _, _, p in _results if p)
    failed = total - passed
    print(f"验证汇总: {passed}/{total} 通过, {failed} 失败")
    print("=" * 70)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
