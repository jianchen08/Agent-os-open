#!/usr/bin/env python
"""手机端构建产物 manifest（G-M0 车道）：三产物 digest 清单的写面与验面。

schema（artifacts_manifest.json）：
    {
      "schema_version": 1,
      "built_at": "2026-10-13T08:00:00+00:00",   # ISO8601 带时区
      "target_arch": "aarch64" | "x86_64",        # 换元两档
      "version": "<构建版本号>",
      "artifacts": {
        "kernel": {"path": "...", "sha256": "<64hex>", "bytes": 123},
        "proot":  {"path": "...", "sha256": "<64hex>", "bytes": 123},
        "rootfs": {"path": "...", "sha256": "<64hex>", "bytes": 123}
      }
    }

契约（与 tests/test_mobile_artifacts_manifest.py 对齐）：
- 三产物键固定 kernel/proot/rootfs，多键少键一律拒绝（fail-closed）；
- target_arch 只认 ALLOWED_ARCHS 两档；
- 验面（verify）对 digest/bytes 不匹配、产物文件缺失逐项报 problem，
  空列表 = 通过；digest 不匹配 = 调用方必须拒绝启动/拒绝分发（本模块
  负责判定与报告，"拒绝"动作归调用方——CLI 退出码非零、启动面集成归
  G-M2 AVD 车道与装机步验收）。
- 相对路径相对 manifest 所在目录解析（base_dir 可显式覆盖）。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ARTIFACT_KEYS: tuple[str, ...] = ("kernel", "proot", "rootfs")
ALLOWED_ARCHS: tuple[str, ...] = ("aarch64", "x86_64")
ARTIFACT_FIELDS: tuple[str, ...] = ("path", "sha256", "bytes")

_MANIFEST_NAME = "artifacts_manifest.json"


class ManifestError(ValueError):
    """manifest schema 违约（写面参数非法 / 读面字段非法），fail-closed。"""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_artifacts_arg(artifacts: Mapping[str, Path]) -> None:
    keys = set(artifacts)
    expected = set(ARTIFACT_KEYS)
    if keys != expected:
        raise ManifestError(f"artifacts 键必须恰为 {sorted(expected)}（三产物缺一不可），实际: {sorted(keys)}")
    for name, path in artifacts.items():
        if not path.is_file():
            raise ManifestError(f"产物不存在: {name} -> {path}")


def build_manifest(
    artifacts: Mapping[str, Path],
    target_arch: str,
    version: str,
    *,
    built_at: str | None = None,
) -> dict[str, Any]:
    """从产物文件计算 digest 组装 manifest dict（不落盘）。

    built_at 供测试注入固定时间；缺省取当前 UTC 时间（ISO8601 带时区）。
    """
    if target_arch not in ALLOWED_ARCHS:
        raise ManifestError(f"target_arch 只认 {list(ALLOWED_ARCHS)}，实际: {target_arch!r}")
    if not version:
        raise ManifestError("version 不得为空")
    _validate_artifacts_arg(artifacts)
    return {
        "schema_version": SCHEMA_VERSION,
        "built_at": built_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "target_arch": target_arch,
        "version": version,
        "artifacts": {
            name: {
                "path": str(artifacts[name]),
                "sha256": _sha256_file(artifacts[name]),
                "bytes": artifacts[name].stat().st_size,
            }
            for name in ARTIFACT_KEYS
        },
    }


def write_manifest(
    out_path: Path,
    artifacts: Mapping[str, Path],
    target_arch: str,
    version: str,
) -> dict[str, Any]:
    """组装并落盘 manifest，返回写入内容（调用方按需断言）。"""
    manifest = build_manifest(artifacts, target_arch, version)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def load_manifest(manifest_path: Path) -> dict[str, Any]:
    """读取并校验 manifest schema，违约即 ManifestError（fail-closed）。"""
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ManifestError("manifest 顶层必须是对象")
    for field in ("schema_version", "built_at", "target_arch", "version", "artifacts"):
        if field not in raw:
            raise ManifestError(f"manifest 缺必填字段: {field}")
    if raw["schema_version"] != SCHEMA_VERSION:
        raise ManifestError(f"schema_version 只认 {SCHEMA_VERSION}，实际: {raw['schema_version']!r}")
    if raw["target_arch"] not in ALLOWED_ARCHS:
        raise ManifestError(f"target_arch 只认 {list(ALLOWED_ARCHS)}，实际: {raw['target_arch']!r}")
    if not isinstance(raw["version"], str) or not raw["version"]:
        raise ManifestError("version 必须是非空字符串")
    entries = raw["artifacts"]
    if not isinstance(entries, dict) or set(entries) != set(ARTIFACT_KEYS):
        raise ManifestError(
            f"artifacts 键必须恰为 {sorted(ARTIFACT_KEYS)}（缺一不可），实际: "
            f"{sorted(entries) if isinstance(entries, dict) else type(entries).__name__}"
        )
    for name, entry in entries.items():
        if not isinstance(entry, dict):
            raise ManifestError(f"artifacts.{name} 必须是对象")
        missing = [f for f in ARTIFACT_FIELDS if f not in entry]
        if missing:
            raise ManifestError(f"artifacts.{name} 缺字段: {missing}")
        digest = entry["sha256"]
        if not isinstance(digest, str) or len(digest) != 64:
            raise ManifestError(f"artifacts.{name}.sha256 必须是 64 位 hex 字符串")
        if not isinstance(entry["bytes"], int) or entry["bytes"] < 0:
            raise ManifestError(f"artifacts.{name}.bytes 必须是非负整数")
        if not isinstance(entry["path"], str) or not entry["path"]:
            raise ManifestError(f"artifacts.{name}.path 必须是非空字符串")
    return raw


def _resolve(base_dir: Path, base_dir_override: Path | None, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path
    root = base_dir_override if base_dir_override is not None else base_dir
    return root / path


def verify_manifest(
    manifest_path: Path,
    base_dir_override: Path | None = None,
) -> list[str]:
    """验面：逐产物核对文件存在性/digest/字节数，返回 problem 列表（空=通过）。

    schema 违约不在本函数容忍范围——先经 load_manifest fail-closed 抛错，
    本函数只报告"内容与实物不符"类 problem。
    """
    manifest = load_manifest(manifest_path)
    base_dir = manifest_path.resolve().parent
    problems: list[str] = []
    for name in ARTIFACT_KEYS:
        entry = manifest["artifacts"][name]
        path = _resolve(base_dir, base_dir_override, entry["path"])
        if not path.is_file():
            problems.append(f"{name}: 产物文件缺失: {entry['path']}")
            continue
        actual_digest = _sha256_file(path)
        if actual_digest != entry["sha256"]:
            # 内容已变时字节数检查是噪音，不再叠加——一项违约一条 problem
            problems.append(f"{name}: digest 不匹配（manifest={entry['sha256']} 实际={actual_digest}）")
            continue
        actual_bytes = path.stat().st_size
        if actual_bytes != entry["bytes"]:
            problems.append(f"{name}: 字节数不匹配（manifest={entry['bytes']} 实际={actual_bytes}）")
    return problems


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="手机端构建产物 manifest 写面/验面")
    sub = parser.add_subparsers(dest="command", required=True)

    p_write = sub.add_parser("write", help="计算三产物 digest 并写出 manifest")
    p_write.add_argument("--out", type=Path, required=True, help="manifest 输出路径")
    p_write.add_argument("--arch", required=True, choices=list(ALLOWED_ARCHS))
    p_write.add_argument("--version", required=True)
    p_write.add_argument(
        "--artifact",
        action="append",
        required=True,
        metavar="KEY=PATH",
        help=f"产物路径，KEY ∈ {list(ARTIFACT_KEYS)}，可重复",
    )

    p_verify = sub.add_parser("verify", help="校验 manifest 与实物 digest，不符退出码非零")
    p_verify.add_argument("--manifest", type=Path, required=True)
    p_verify.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="相对产物路径的解析基准（缺省=manifest 所在目录）",
    )

    args = parser.parse_args(argv)
    if args.command == "write":
        artifacts: dict[str, Path] = {}
        for item in args.artifact:
            key, sep, value = item.partition("=")
            if not sep:
                parser.error(f"--artifact 须为 KEY=PATH 形式: {item}")
            if key not in ARTIFACT_KEYS:
                parser.error(f"--artifact 键只认 {list(ARTIFACT_KEYS)}: {key}")
            if key in artifacts:
                parser.error(f"--artifact 键重复: {key}")
            artifacts[key] = Path(value)
        manifest = write_manifest(args.out, artifacts, args.arch, args.version)
        print(json.dumps({"written": str(args.out), "target_arch": manifest["target_arch"]}))
        return 0

    problems = verify_manifest(args.manifest, args.base_dir)
    if problems:
        for problem in problems:
            print(f"[FAIL] {problem}", file=sys.stderr)
        return 1
    print(f"[OK] manifest 校验通过: {args.manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
