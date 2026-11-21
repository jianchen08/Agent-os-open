#!/usr/bin/env python
"""部署回读校验：发版清单生成 + 「部署位实际字节」比对。

设计：docs/decisions/2026-11-10-deploy-freshness-verification.md

两类使用面（一次生成，四处回读）：
- **打包期 build**：对 electron-builder 最终产物树（afterPack 钩子）/ Linux 云包
  快照逐文件 sha256，生成随包下发的 ``deploy-manifest.json``；
- **部署期 verify**：回读部署位实际文件与清单比对——缺文件 / 哈希不符 /
  （严格档）多余文件即 red，回答「本次发版的 diff 与二进制产物是否真的部署
  到位」。关键项（内核 exe、app.asar、各 plugin.json、frontend index、config
  内核配置、_host 宿主）缺失或不符时调用方必须拦启动/拦部署。

清单 schema（1）::

    {schema, profile, version, build_head, generated_at,
     managed_roots: [rel...], critical: [rel...],
     files: {<posix rel>: {sha256, bytes}}}

排除面（与 electron/plugin-tier2-provision.ts hashPluginTree、
scripts/sync_installed_user_space.py _hash_plugin_tree 同口径）：目录名
node_modules/__pycache__/.mypy_cache/.pytest_cache/.ruff_cache/target/.git、
``.venv`` 与 ``.venv-*``、``*.pyc``；符号链接/junction 不入清单（Windows
junction 存绝对路径，跨机必断）。``.venv`` 是环境产物且体积占大头，不进清单
——venv 健康由 patrolPluginVenvs / check_plugin_runtime_health 兜底（ADR 局限节）。

运行期自写入路径从「多余文件」判定豁免（kernel/logs/、plugins/**/data/）：
本脚本只判部署存量，运行期产物不算旧版本残留。

退出码：0 = 通过（非严格档非关键 finding 只告警）；1 = 存在 finding（严格档
任何 finding / 非严格档关键项 finding）；2 = 环境不可判（root/manifest 缺失、
schema 违约、profile 未知、root 内无可校验文件）。

用法::

    python deploy/verify_deploy_manifest.py build \
        --root release/win-unpacked/resources \
        --out release/win-unpacked/resources/deploy-manifest.json \
        --profile windows --version 0.2.2 [--build-head <sha>]

    python deploy/verify_deploy_manifest.py verify --root <部署位> \
        --manifest <清单> [--strict] [--json] [--baseline <上一版清单>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1
MANIFEST_NAME = "deploy-manifest.json"

#: profile -> 参与清单的部署面根目录（相对部署根）。
#: windows = electron-builder resources/ 布局；linux = 云包部署根（deploy/ 随包，
#: 内核由服务器 build.sh 现编故不在清单内，由 verify_deploy.sh 另行断言）。
PROFILE_ROOTS: dict[str, tuple[str, ...]] = {
    "windows": ("bin", "config", "frontend", "kernel", "plugins", "prereqs", "skills"),
    "linux": ("plugins/shared", "plugins/sdk", "config", "skills", "frontend/dist", "deploy"),
}

#: 部署根顶层、但在 managed roots 之外的必检文件（electron-builder 生成的壳产物）。
PROFILE_ROOT_FILES: dict[str, tuple[str, ...]] = {
    "windows": ("app.asar",),
    "linux": (),
}

EXCLUDED_DIR_NAMES = frozenset(
    {"node_modules", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "target", ".git"}
)
EXCLUDED_DIR_PREFIXES = (".venv",)
EXCLUDED_FILE_SUFFIXES = (".pyc",)

#: 关键项：缺失/哈希不符 → 应用或插件面必挂（Windows 首启拦、部署链拦）。
CRITICAL_EXACT = frozenset(
    {
        "app.asar",
        "kernel/agentos-kernel.exe",
        "kernel/agentos-kernel",
        "frontend/dist/index.html",
        "plugins/shared/_host/host.py",
    }
)
CRITICAL_FILE_NAMES = frozenset({"plugin.json"})
#: config/kernel/*.yaml（instance.yaml 缺失内核 fail-closed 拒启）。
CRITICAL_PREFIX = "config/kernel/"
CRITICAL_SUFFIX = ".yaml"

#: 运行期自写入面：只从「多余文件」判定豁免，清单本身照常收录。
RUNTIME_EXTRA_PREFIXES = ("kernel/logs/",)
RUNTIME_DATA_DIR = "data"


class ManifestError(ValueError):
    """manifest 生成/读取 schema 违约，fail-closed。"""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_excluded(name: str, *, is_dir: bool) -> bool:
    if is_dir:
        return name in EXCLUDED_DIR_NAMES or name.startswith(EXCLUDED_DIR_PREFIXES)
    return name.endswith(EXCLUDED_FILE_SUFFIXES)


def iter_tree_files(root: Path, rel_root: str) -> Iterator[tuple[str, Path]]:
    """遍历 root/rel_root 下的常规文件，产出 (posix 相对路径, 绝对路径)。

    符号链接与 junction 一律跳过（不入清单，也不参与多余判定）。
    """
    base = root / rel_root if rel_root else root
    stack: list[tuple[Path, str]] = [(base, rel_root)]
    while stack:
        directory, rel_dir = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                rel = f"{rel_dir}/{entry.name}" if rel_dir else entry.name
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if not _is_excluded(entry.name, is_dir=True):
                        stack.append((Path(entry.path), rel))
                elif entry.is_file(follow_symlinks=False):
                    if not _is_excluded(entry.name, is_dir=False):
                        yield rel, Path(entry.path)


def is_critical(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    if rel in CRITICAL_EXACT or name in CRITICAL_FILE_NAMES:
        return True
    return rel.startswith(CRITICAL_PREFIX) and rel.endswith(CRITICAL_SUFFIX)


def build_manifest(
    root: Path,
    *,
    profile: str,
    version: str,
    build_head: str = "",
    generated_at: str | None = None,
    skip_paths: tuple[Path, ...] = (),
) -> dict[str, Any]:
    """对 root 按 profile 的部署面根目录生成清单 dict（不落盘）。"""
    if profile not in PROFILE_ROOTS:
        raise ManifestError(f"profile 只认 {sorted(PROFILE_ROOTS)}，实际: {profile!r}")
    if not version:
        raise ManifestError("version 不得为空")
    roots = PROFILE_ROOTS[profile]
    missing_roots = [r for r in roots if not (root / r).is_dir()]
    if missing_roots:
        raise ManifestError(f"部署面根目录缺失（{profile}）: {missing_roots}")

    skip = {p.resolve() for p in skip_paths}
    files: dict[str, dict[str, Any]] = {}
    for rel in PROFILE_ROOT_FILES[profile]:
        path = root / rel
        if not path.is_file():
            raise ManifestError(f"部署根顶层必检文件缺失（{profile}）: {rel}")
        files[rel] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    for rel_root in roots:
        for rel, path in iter_tree_files(root, rel_root):
            if path.resolve() in skip:
                continue
            files[rel] = {"sha256": sha256_file(path), "bytes": path.stat().st_size}
    if not files:
        raise ManifestError(f"root 内无可校验文件: {root}")

    ordered = {key: files[key] for key in sorted(files)}
    return {
        "schema": SCHEMA_VERSION,
        "profile": profile,
        "version": version,
        "build_head": build_head,
        "generated_at": generated_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "managed_roots": list(roots),
        "critical": [rel for rel in ordered if is_critical(rel)],
        "files": ordered,
    }


def write_manifest(out_path: Path, manifest: dict[str, Any]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    """读取并校验清单 schema，违约即 ManifestError（fail-closed）。"""
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"清单不可读: {manifest_path}（{exc}）") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"清单不是合法 JSON: {manifest_path}（{exc}）") from exc
    if not isinstance(raw, dict):
        raise ManifestError("清单顶层必须是对象")
    for field in ("schema", "profile", "version", "build_head", "managed_roots", "critical", "files"):
        if field not in raw:
            raise ManifestError(f"清单缺必填字段: {field}")
    if raw["schema"] != SCHEMA_VERSION:
        raise ManifestError(f"schema 只认 {SCHEMA_VERSION}，实际: {raw['schema']!r}")
    if raw["profile"] not in PROFILE_ROOTS:
        raise ManifestError(f"profile 只认 {sorted(PROFILE_ROOTS)}，实际: {raw['profile']!r}")
    if not isinstance(raw["version"], str) or not raw["version"]:
        raise ManifestError("version 必须是非空字符串")
    if not isinstance(raw["build_head"], str):
        raise ManifestError("build_head 必须是字符串（本地包可为空串）")
    if not isinstance(raw["managed_roots"], list) or not all(isinstance(r, str) for r in raw["managed_roots"]):
        raise ManifestError("managed_roots 必须是字符串数组")
    files = raw["files"]
    if not isinstance(files, dict) or not files:
        raise ManifestError("files 必须是非空对象")
    for rel, entry in files.items():
        if not isinstance(entry, dict):
            raise ManifestError(f"files.{rel} 必须是对象")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ManifestError(f"files.{rel}.sha256 必须是 64 位 hex 字符串")
        size = entry.get("bytes")
        if not isinstance(size, int) or size < 0:
            raise ManifestError(f"files.{rel}.bytes 必须是非负整数")
    critical = raw["critical"]
    if not isinstance(critical, list) or not all(isinstance(r, str) for r in critical):
        raise ManifestError("critical 必须是字符串数组")
    stray = [rel for rel in critical if rel not in files]
    if stray:
        raise ManifestError(f"critical 条目不在 files 内: {stray}")
    return raw


@dataclass(frozen=True)
class Finding:
    """单条回读结论；kind ∈ missing / mismatch / extra。"""

    kind: str
    path: str
    critical: bool
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_runtime_written(rel: str) -> bool:
    if rel.startswith(RUNTIME_EXTRA_PREFIXES):
        return True
    parts = rel.split("/")
    return len(parts) > 1 and parts[0] == "plugins" and RUNTIME_DATA_DIR in parts[1:-1]


def _extra_files(root: Path, manifest: dict[str, Any]) -> list[str]:
    known = set(manifest["files"])
    extras: list[str] = []
    for rel_root in manifest["managed_roots"]:
        if not (root / rel_root).is_dir():
            continue
        for rel, _path in iter_tree_files(root, rel_root):
            if rel in known or rel.rsplit("/", 1)[-1] == MANIFEST_NAME:
                continue
            if _is_runtime_written(rel):
                continue
            extras.append(rel)
    return extras


def verify_deployment(
    root: Path,
    manifest: dict[str, Any],
    *,
    check_extra: bool = False,
) -> list[Finding]:
    """回读 root 与清单比对；返回 findings（空列表 = 全绿）。"""
    files = manifest["files"]
    critical = set(manifest["critical"])
    findings: list[Finding] = []
    for rel in sorted(files):
        path = root / rel
        if not path.is_file():
            findings.append(Finding("missing", rel, rel in critical, "清单有条目但部署位不存在"))
            continue
        actual = sha256_file(path)
        expected = files[rel]["sha256"]
        if actual != expected:
            findings.append(
                Finding("mismatch", rel, rel in critical, f"期望 {expected[:16]}… 实际 {actual[:16]}…")
            )
    if check_extra:
        for rel in sorted(_extra_files(root, manifest)):
            findings.append(Finding("extra", rel, False, "部署位存在但清单无此条目（旧版本残留）"))
    return findings


def changed_paths(prev: dict[str, Any], cur: dict[str, Any]) -> list[str]:
    """两版清单间的变更集（新增 / 删除 / 哈希变化），用于「本次 diff」报告。"""
    prev_files = prev["files"]
    cur_files = cur["files"]
    changed: list[str] = []
    for rel in sorted(set(prev_files) | set(cur_files)):
        before = prev_files.get(rel, {}).get("sha256")
        after = cur_files.get(rel, {}).get("sha256")
        if before != after:
            changed.append(rel)
    return changed


def _emit(findings: list[Finding], strict: bool, changed: list[str] | None) -> int:
    critical_hits = [f for f in findings if f.critical]
    if changed is not None:
        changed_set = set(changed)
        landed = [rel for rel in changed if rel in changed_set]
        failed_changed = [f.path for f in findings if f.path in changed_set]
        print(
            f"[deploy-verify] 本次更新变更 {len(landed)} 项，"
            f"其中未落地 {len(failed_changed)} 项"
        )
    if not findings:
        print("[deploy-verify] PASS 部署位与发版清单一致")
        return 0
    kinds: dict[str, int] = {}
    for finding in findings:
        kinds[finding.kind] = kinds.get(finding.kind, 0) + 1
    summary = " / ".join(f"{kind} {count}" for kind, count in sorted(kinds.items()))
    verdict = "FAIL" if strict or critical_hits else "WARN"
    print(f"[deploy-verify] {verdict} {len(findings)} 项（{summary}；关键项 {len(critical_hits)}）")
    for finding in findings:
        flag = "critical" if finding.critical else "        "
        print(f"  [{finding.kind:8}] [{flag}] {finding.path}  {finding.detail}")
    return 1 if strict or critical_hits else 0


def _cmd_build(args: argparse.Namespace) -> int:
    root = Path(args.root)
    out = Path(args.out)
    if not root.is_dir():
        print(f"[deploy-verify][ERROR] --root 不存在: {root}", file=sys.stderr)
        return 2
    try:
        manifest = build_manifest(
            root,
            profile=args.profile,
            version=args.version,
            build_head=args.build_head or "",
            skip_paths=(out,),
        )
        write_manifest(out, manifest)
    except ManifestError as exc:
        print(f"[deploy-verify][ERROR] {exc}", file=sys.stderr)
        return 2
    print(
        f"[deploy-verify] 清单已生成 {out}（profile={args.profile} "
        f"files={len(manifest['files'])} critical={len(manifest['critical'])}）"
    )
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    root = Path(args.root)
    manifest_path = Path(args.manifest)
    if not root.is_dir():
        print(f"[deploy-verify][ERROR] --root 不存在: {root}", file=sys.stderr)
        return 2
    try:
        manifest = load_manifest(manifest_path)
        baseline = load_manifest(Path(args.baseline)) if args.baseline else None
    except ManifestError as exc:
        print(f"[deploy-verify][ERROR] {exc}", file=sys.stderr)
        return 2
    findings = verify_deployment(root, manifest, check_extra=args.strict)
    changed = changed_paths(baseline, manifest) if baseline is not None else None
    code = _emit(findings, args.strict, changed)
    if args.json:
        print(
            json.dumps(
                {
                    "ok": code == 0,
                    "version": manifest["version"],
                    "build_head": manifest["build_head"],
                    "findings": [f.as_dict() for f in findings],
                    "changed": changed,
                },
                ensure_ascii=False,
            )
        )
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="部署回读校验（发版清单 build / 部署位 verify）")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="对产物树生成 deploy-manifest.json")
    build.add_argument("--root", required=True, help="部署根（Windows=resources，Linux=云包根）")
    build.add_argument("--out", required=True, help="清单输出路径")
    build.add_argument("--profile", default="windows", choices=sorted(PROFILE_ROOTS))
    build.add_argument("--version", required=True, help="VERSION 单源值")
    build.add_argument("--build-head", default="", help="构建 HEAD sha（发布链必带）")
    build.set_defaults(func=_cmd_build)

    verify = sub.add_parser("verify", help="回读部署位并与清单比对")
    verify.add_argument("--root", required=True, help="部署位根")
    verify.add_argument("--manifest", required=True, help="deploy-manifest.json 路径")
    verify.add_argument("--strict", action="store_true", help="任何 finding（含多余文件）即 red")
    verify.add_argument("--json", action="store_true", help="追加机器可读 JSON 输出")
    verify.add_argument("--baseline", help="上一版清单（输出本次 diff 的落地情况）")
    verify.set_defaults(func=_cmd_verify)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
