#!/usr/bin/env python
"""三端统一版本号闸（AR-M10，用户拍板 2026-10-15：三端使用统一版本号）。

单一真值 = 仓根 `VERSION` 文件（严格三段式 `X.Y.Z`）。三端版本引用面必须与
其一致：
  kernel/Cargo.toml                    [workspace.package] version（运行时经
                                       env!("CARGO_PKG_VERSION") 消费）
  package.json                         version（桌面壳声明面：electron-builder
                                       自此处取版本进 NSIS 安装器元数据）
  frontend/package.json                version
  app-mobile/package.json              version
  app-mobile/android/app/build.gradle  versionName "..."（移动端应用版本单点，
                                       syncweb 自此处生成 www/shell-config.js）

`versionCode` 不入闸面：Android 单调递增构建号（平台约束），归移动打包轮
「版本顺延」契约自管，与统一版本号正交。

缺失 / 格式非法 / 漂移 / 多处声明一律 exit 1——fail-closed，防闸退化成
永绿假闸。漂移一次性报全（改 VERSION 后照报错逐面对齐即可）。

用法：
    python scripts/check_version_sync.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
# 抽取面只锚 [workspace.package] 段内的 version 键（Cargo.toml 的
# [workspace.dependencies] 等他段亦含 version 字样，不得误取）。
_CARGO_SECTION_RE = re.compile(r"^\[workspace\.package\]\s*$", re.MULTILINE)
_CARGO_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)
_CARGO_NEXT_SECTION_RE = re.compile(r"^\[", re.MULTILINE)
_GRADLE_VERSION_NAME_RE = re.compile(r'versionName\s+"([^"]+)"')


def _extract_workspace_cargo_version(text: str) -> str | None:
    """取 [workspace.package] 段内首个 version 键；段缺失/键缺失返回 None。"""
    section = _CARGO_SECTION_RE.search(text)
    if section is None:
        return None
    body = text[section.end() :]
    next_section = _CARGO_NEXT_SECTION_RE.search(body)
    if next_section is not None:
        body = body[: next_section.start()]
    found = _CARGO_VERSION_RE.search(body)
    return found.group(1) if found else None


def check_sync(root: Path) -> tuple[str, list[str]]:
    """对仓 `root` 跑五面一致性检查，返回 (VERSION 真值, 错误列表)。

    错误列表为空 = 全部一致（绿）。单面抽取失败与值漂移都进错误列表，
    一次性报全。
    """
    errors: list[str] = []

    version_file = root / "VERSION"
    if not version_file.is_file():
        return "", [f"VERSION 文件缺失: {version_file}"]
    root_version = version_file.read_text(encoding="utf-8").strip()
    if not VERSION_RE.match(root_version):
        return root_version, [f"VERSION 值 {root_version!r} 非严格三段式 X.Y.Z（单源格式契约）"]

    surfaces: list[tuple[str, Path, str | None]] = []

    cargo = root / "kernel" / "Cargo.toml"
    cargo_version = _extract_workspace_cargo_version(cargo.read_text(encoding="utf-8")) if cargo.is_file() else None
    surfaces.append(("kernel/Cargo.toml [workspace.package] version", cargo, cargo_version))

    for rel in ("package.json", "frontend/package.json", "app-mobile/package.json"):
        path = root / rel
        value: str | None = None
        if path.is_file():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                value = None
            else:
                raw = data.get("version") if isinstance(data, dict) else None
                value = raw if isinstance(raw, str) else None
        surfaces.append((f"{rel} version", path, value))

    gradle = root / "app-mobile" / "android" / "app" / "build.gradle"
    gradle_version: str | None = None
    gradle_ambiguous = False
    if gradle.is_file():
        matches = _GRADLE_VERSION_NAME_RE.findall(gradle.read_text(encoding="utf-8"))
        if len(matches) == 1:
            gradle_version = matches[0]
        elif len(matches) > 1:
            gradle_ambiguous = True
            errors.append(
                f"app-mobile/android/app/build.gradle versionName 声明了 {len(matches)} 处"
                f"（{matches!r}），闸面要求唯一声明——歧义须人工归一后重跑"
            )
    if not gradle_ambiguous:
        surfaces.append(("app-mobile/android/app/build.gradle versionName", gradle, gradle_version))

    for label, path, value in surfaces:
        if value is None:
            errors.append(f"{label}: 文件缺失或不可解析（fail-closed）: {path}")
        elif value != root_version:
            errors.append(f"{label}: {value!r} != VERSION {root_version!r}（漂移，请对齐）")
    return root_version, errors


def main() -> int:
    root_version, errors = check_sync(ROOT)
    if errors:
        print(f"VERSION = {root_version!r}，三端版本面漂移：", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        print(
            "对齐方式：改 VERSION 后逐面同步（或改回 VERSION），见台账 docs/working/三端版本统一_台账.md",
            file=sys.stderr,
        )
        return 1
    print(f"版本同步闸通过：VERSION = {root_version}，全部版本引用面一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
