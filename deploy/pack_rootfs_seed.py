#!/usr/bin/env python
"""rootfs 种子打包（G-M0 rootfs 步，deploy/build_mobile.sh 消费）。

Ubuntu base tarball 流式变换 + overlay 树注入 + 附加 tar 注入，产出
rootfs_seed.tar（或 --zstd 时 rootfs_seed.tar.zst）：
- ubuntu-base 原包逐条目保真拷贝——符号链接（bin→usr/bin 等，Windows 宿主
  文件系统无法落盘重建）与 Unix 权限位（MSYS 提取即丢，env 落盘变 644）
  只能经 tar 条目层保真，任何"先解包再重打包"的宿主侧路线在 Windows 上必损；
- 跳过原包 etc/resolv.conf，改注 overlay 版（坑⑲：原值指向 Android netd
  不可达，须写真 IP nameserver）；
- overlay 树（/opt/agentos 部署面 + 预 sync venvs + /usr/local/bin/uv）整体
  注入：目录 755；/usr/local/bin 与 /opt/agentos/deploy 下常规文件 755
  （脚本/二进制），其余文件 644；
- --inject-tar/--inject-prefix：附加 tar（python-build-standalone cpython）
  条目级重映射注入——剥去其顶层目录、前缀换为 --inject-prefix，模式位与
  链接逐条目保真（宿主侧解包同样会丢链接）；
- --dedup：overlay 常规文件内容寻址去重——同内容（size 分桶 + sha256）的
  后续条目以硬链接（LNKTYPE）落包，指向排序最小的同内容路径。15 份 venv
  的 cryptography/pydantic-core 等 C 扩展跨 venv 逐字节同源，去重直接砍种子
  真实落盘体积；设备端修复路径（uv sync）按"整体替换"语义写文件，别名
  共享不引入改写串扰。
- --zstd：输出经 zstd 流式压缩（level 可调）——venv/site-packages 压缩比
  高，APK 内种子资产按预压缩形态携带（aapt noCompress）。

用法：
  python deploy/pack_rootfs_seed.py \
      --ubuntu <ubuntu-base.tar.gz> --overlay <overlay-root> \
      --out <rootfs_seed.tar> [--resolv-conf <文件>] \
      [--inject-tar <cpython.tar.gz> --inject-prefix opt/agentos/python] \
      [--dedup] [--zstd]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import tarfile
from pathlib import Path, PurePosixPath

_SKIP_SOURCE = {"etc/resolv.conf"}
_DIR_MODE = 0o755
_FILE_MODE = 0o644
_EXEC_SUBTREES = ("usr/local/bin", "opt/agentos/deploy", "opt/agentos/agentos-kernel")
_DEDUP_MIN_SIZE = 1  # 0 字节文件无收益，不参与去重


def _norm(name: str) -> str:
    posix = str(PurePosixPath(name))
    return posix[2:] if posix.startswith("./") else posix


def _overlay_entries(overlay_root: Path):
    """确定性序（sorted）铺开 overlay 树为 (posix 路径, Path, 是否目录)。"""
    entries: list[tuple[str, Path, bool]] = []
    for path in sorted(overlay_root.rglob("*")):
        rel = path.relative_to(overlay_root).as_posix()
        entries.append((rel, path, path.is_dir()))
    return entries


def _dedup_map(overlay_root: Path, entries) -> tuple[dict[str, str], int]:
    """内容寻址去重映射：rel → 其硬链接目标（组内**出流序第一者**）。

    size 分桶后仅对同尺寸 ≥2 的文件算 sha256（大半文件唯一，避免全量哈希）。
    canonical 必须取条目流（sorted entries）中位置最早者——tar 硬链接只许
    **后向引用**；用字符串 min 选 canonical 会与 Path 排序（parts 元组序，
    前缀规则）不一致，产生前向引用，GNU tar / tar-rs 双双报目标不存在
    （R5 实测坑㉚）。返回 (dup→canonical 映射, 可省字节数)。
    """
    order = {rel: i for i, (rel, _p, _d) in enumerate(entries)}
    by_size: dict[int, list[tuple[str, Path]]] = {}
    for rel, path, is_dir in entries:
        if is_dir:
            continue
        size = path.stat().st_size
        if size < _DEDUP_MIN_SIZE:
            continue
        by_size.setdefault(size, []).append((rel, path))
    groups: dict[str, str] = {}
    saved = 0
    for size, items in by_size.items():
        if len(items) < 2:
            continue
        hashes: dict[str, list[str]] = {}
        for rel, path in items:
            digest = hashlib.sha256()
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    digest.update(chunk)
            hashes.setdefault(digest.hexdigest(), []).append(rel)
        for rels in hashes.values():
            if len(rels) < 2:
                continue
            canonical = min(rels, key=lambda r: order[r])
            for rel in rels:
                if rel != canonical:
                    groups[rel] = canonical
                    saved += size
    return groups, saved


def pack(
    ubuntu_tar: Path,
    overlay_root: Path,
    out_tar: Path,
    inject_tar: Path | None = None,
    inject_prefix: str = "",
    dedup: bool = False,
    zstd_level: int | None = None,
) -> dict[str, int]:
    copied = 0
    skipped = 0
    overlay = 0
    injected = 0

    entries = _overlay_entries(overlay_root)
    dup_map: dict[str, str] = {}
    dedup_saved = 0
    if dedup:
        dup_map, dedup_saved = _dedup_map(overlay_root, entries)

    out_fileobj = None
    close_ctx = None
    if zstd_level is not None:
        import zstandard

        # 流式保真链（fileobj→zstd writer→tar）生命期由下方 finally 统一关闭，
        # 无法用单一 with 表达——SIM115 豁免。
        out_fileobj = open(out_tar, "wb")  # noqa: SIM115
        close_ctx = zstandard.ZstdCompressor(level=zstd_level).stream_writer(out_fileobj)
        dst_open = lambda: tarfile.open(  # noqa: E731, SIM115
            fileobj=close_ctx, mode="w", format=tarfile.GNU_FORMAT
        )
    else:
        dst_open = lambda: tarfile.open(out_tar, "w", format=tarfile.GNU_FORMAT)  # noqa: E731, SIM115
    try:
        with tarfile.open(ubuntu_tar, "r|gz") as src, dst_open() as dst:
            for info in src:
                norm = _norm(info.name)
                if norm in _SKIP_SOURCE or norm == "etc/resolv.conf":
                    skipped += 1
                    continue
                if info.isfile():
                    dst.addfile(info, src.extractfile(info))
                else:
                    # 目录/符号链接/硬链接等非数据条目原样拷贝（链接不经宿主 fs）
                    dst.addfile(info)
                copied += 1
            for rel, path, is_dir in entries:
                if is_dir:
                    info = tarfile.TarInfo(rel)
                    info.type = tarfile.DIRTYPE
                    info.mode = _DIR_MODE
                    info.size = 0
                    dst.addfile(info)
                elif rel in dup_map:
                    info = tarfile.TarInfo(rel)
                    info.type = tarfile.LNKTYPE
                    info.linkname = dup_map[rel]
                    info.size = 0
                    dst.addfile(info)
                else:
                    exec_tree = any(rel == sub or rel.startswith(sub + "/") for sub in _EXEC_SUBTREES)
                    info = tarfile.TarInfo(rel)
                    info.type = tarfile.REGTYPE
                    info.mode = 0o755 if exec_tree else _FILE_MODE
                    data = path.read_bytes()
                    info.size = len(data)
                    dst.addfile(info, io.BytesIO(data))
                overlay += 1
            if inject_tar is not None:
                with tarfile.open(inject_tar, "r|gz") as inj:
                    for info in inj:
                        norm = _norm(info.name)
                        parts = norm.split("/", 1)
                        info.name = f"{inject_prefix}/{parts[1]}" if len(parts) == 2 else inject_prefix
                        if info.isfile():
                            dst.addfile(info, inj.extractfile(info))
                        else:
                            dst.addfile(info)
                        injected += 1
    finally:
        if close_ctx is not None:
            close_ctx.close()
            if out_fileobj is not None:
                out_fileobj.close()
    return {
        "source_entries": copied,
        "source_skipped": skipped,
        "overlay_entries": overlay,
        "inject_entries": injected,
        "dedup_files": len(dup_map),
        "dedup_saved_bytes": dedup_saved,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="rootfs 种子打包（ubuntu 流式保真 + overlay 注入）")
    parser.add_argument("--ubuntu", type=Path, required=True, help="ubuntu-base tar.gz 输入")
    parser.add_argument("--overlay", type=Path, required=True, help="overlay 树根（注入到 /；含 etc/resolv.conf）")
    parser.add_argument("--out", type=Path, required=True, help="输出 tar（--zstd 时为 .tar.zst）")
    parser.add_argument(
        "--inject-tar",
        type=Path,
        default=None,
        help="附加注入的 tar.gz（逐条目保真，剥顶层目录后挂到 --inject-prefix）",
    )
    parser.add_argument("--inject-prefix", default="", help="--inject-tar 条目的挂载前缀（如 opt/agentos/python）")
    parser.add_argument("--dedup", action="store_true", help="overlay 常规文件内容寻址硬链接去重")
    parser.add_argument("--zstd", action="store_true", help="输出 zstd 流式压缩（.tar.zst）")
    parser.add_argument("--zstd-level", type=int, default=7, help="zstd 压缩级别（默认 7）")
    args = parser.parse_args(argv)
    if not args.ubuntu.is_file():
        parser.error(f"ubuntu tar 不存在: {args.ubuntu}")
    if not args.overlay.is_dir():
        parser.error(f"overlay 根不存在: {args.overlay}")
    if args.inject_tar is not None and not args.inject_prefix:
        parser.error("--inject-tar 需配 --inject-prefix")
    level = args.zstd_level if args.zstd else None
    stats = pack(
        args.ubuntu,
        args.overlay,
        args.out,
        inject_tar=args.inject_tar,
        inject_prefix=args.inject_prefix,
        dedup=args.dedup,
        zstd_level=level,
    )
    print(
        f"[pack-rootfs] out={args.out} source={stats['source_entries']} "
        f"(skipped={stats['source_skipped']}) overlay={stats['overlay_entries']} "
        f"inject={stats['inject_entries']} dedup_files={stats['dedup_files']} "
        f"dedup_saved_mb={stats['dedup_saved_bytes'] / 1024 / 1024:.1f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
