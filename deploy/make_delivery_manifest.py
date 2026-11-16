#!/usr/bin/env python
"""统一交付清单生成器（全客户端统一发布管线，deploy/release_all.sh 末段消费）。

对 release/<版本>/ 目录下的全客户端产物出一份 delivery_manifest.json：
  schema_version / released_at / build_head /
  version: {semver, version_code} /
  artifacts: {<名称>: {path, sha256, bytes}}

- path 相对 manifest 所在目录（可搬移产物包语义，与 artifacts_manifest.py 同律）；
- sha256 流式计算（产物 250MB 级，禁整体读入内存）；
- fail-closed：产物文件缺失 / 重复名称 / semver 格式非法 / version_code 非负整数
  一律 exit 2，不产出半份清单。

用法：
    python deploy/make_delivery_manifest.py \
        --out  release/0.2.1/delivery_manifest.json \
        --semver 0.2.1 --version-code 24 --build-head <sha> \
        --artifact apk-x86_64=release/0.2.1/lingxi-agentos-x86_64-mumu.apk \
        --artifact ...（可重复）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    out_dir: Path,
    semver: str,
    version_code: int,
    build_head: str,
    artifacts: list[tuple[str, Path]],
    released_at: str | None = None,
) -> dict:
    if not SEMVER_RE.match(semver):
        raise ValueError(f"semver {semver!r} 非严格三段式 X.Y.Z")
    if version_code < 0:
        raise ValueError(f"version_code {version_code} 非负整数要求")
    if not build_head:
        raise ValueError("build_head 缺失（发布必须可溯源到构建 HEAD）")

    entries: dict[str, dict] = {}
    for name, path in artifacts:
        if name in entries:
            raise ValueError(f"产物名称重复: {name}")
        if not path.is_file():
            raise ValueError(f"产物文件缺失: {path}")
        try:
            rel = path.resolve().relative_to(out_dir.resolve()).as_posix()
        except ValueError:
            rel = str(path)
        entries[name] = {
            "path": rel,
            "sha256": sha256_file(path),
            "bytes": path.stat().st_size,
        }

    return {
        "schema_version": 1,
        "released_at": released_at or datetime.now(UTC).isoformat(timespec="seconds"),
        "build_head": build_head,
        "version": {"semver": semver, "version_code": version_code},
        "artifacts": entries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True, help="manifest 输出路径")
    parser.add_argument("--semver", required=True, help="VERSION 单源值（X.Y.Z）")
    parser.add_argument("--version-code", required=True, type=int, help="安卓单调构建号")
    parser.add_argument("--build-head", required=True, help="构建 HEAD sha")
    parser.add_argument(
        "--artifact",
        action="append",
        required=True,
        metavar="名称=路径",
        help="产物条目（可重复）",
    )
    args = parser.parse_args()

    artifacts: list[tuple[str, Path]] = []
    for item in args.artifact:
        name, sep, value = item.partition("=")
        if not sep or not name or not value:
            print(f"[delivery-manifest][ERROR] --artifact 形态须为 名称=路径: {item!r}", file=sys.stderr)
            return 2
        artifacts.append((name, Path(value)))

    out = Path(args.out)
    try:
        manifest = build_manifest(out.parent, args.semver, args.version_code, args.build_head, artifacts)
        out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except ValueError as exc:
        print(f"[delivery-manifest][ERROR] {exc}", file=sys.stderr)
        return 2
    print(f"[delivery-manifest] OK {out}（artifacts={len(manifest['artifacts'])}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
