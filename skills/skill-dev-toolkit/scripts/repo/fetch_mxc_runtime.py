"""MXC 运行时取件：按 pinned 清单从发行 zip 中只取 x64 执行器文件。

设计真值：``docs/working/MXC隔离后端_设计草案_20261117.md`` §18。
MXC 发行 zip 509MB，而打包只需要 ~33MB 的 x64 可执行件；本脚本用 HTTP Range
读 zip 尾部中央目录，再按成员偏移逐个取压缩块解压，校验 pin 的 SHA-256 后落
``build/mxc-runtime``（electron-builder extraResources 复制到 resources/mxc）。

用法：
  python scripts/fetch_mxc_runtime.py              # 取件（幂等，已齐则跳过）
  python scripts/fetch_mxc_runtime.py --force      # 强制重取
  python scripts/fetch_mxc_runtime.py --check      # 只校验已有产物
  python scripts/fetch_mxc_runtime.py --out DIR --manifest FILE

网络：默认走 GitHub API 资产端点（api.github.com 重定向到 release-assets，
Range 可穿透）；设置 ``GITHUB_TOKEN`` 提升配额，``--source-url`` 可换镜像
（仍需与 pin 的 SHA-256 一致）。失败即非零退出，不静默产缺包。
"""

from __future__ import annotations

import argparse
import binascii
import hashlib
import json
import os
import shutil
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = REPO_ROOT / "scripts" / "mxc_runtime.json"
DEFAULT_OUT = REPO_ROOT / "build" / "mxc-runtime"
_TAIL_BYTES = 512 * 1024
_RETRIES = 5
_TIMEOUT_SECONDS = 120


class FetchError(RuntimeError):
    """取件失败（网络/校验/格式）。"""


def _http_range(url: str, start: int, end: int, *, token: str | None) -> bytes:
    """HTTP Range 取 [start, end]（含端点）字节；重定向保留 Range。"""
    if end < start:
        raise FetchError(f"非法 Range: {start}-{end}")
    headers = {
        "Accept": "application/octet-stream",
        "Range": f"bytes={start}-{end}",
        "User-Agent": "AgentOS-Build/mxc-runtime",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    expected = end - start + 1
    last_error: Exception | None = None
    for attempt in range(1, _RETRIES + 1):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                if response.status != 206:
                    raise FetchError(
                        f"服务器未按 Range 返回（status={response.status}, range={start}-{end}）"
                    )
                blob = response.read()
            if len(blob) != expected:
                raise FetchError(f"Range 长度不符: {len(blob)} != {expected}")
            return blob
        except (urllib.error.URLError, TimeoutError, OSError, FetchError) as exc:
            last_error = exc
            if attempt < _RETRIES:
                time.sleep(2 * attempt)
    raise FetchError(f"Range 取件失败 {start}-{end}: {last_error}")


def _parse_entries(cd: bytes, n_total: int) -> list[dict[str, Any]]:
    """解析中央目录条目数组。"""
    entries: list[dict[str, Any]] = []
    cursor = 0
    for _ in range(n_total):
        if cd[cursor : cursor + 4] != b"PK\x01\x02":
            raise FetchError(f"中央目录签名非法 @ {cursor}")
        fields = struct.unpack_from("<4s6H3L5H2L", cd, cursor)
        name_len, extra_len, comment_len = fields[10], fields[11], fields[12]
        if fields[4] not in (0, 8):
            raise FetchError(f"不支持的压缩方法 {fields[4]}")
        name = cd[cursor + 46 : cursor + 46 + name_len].decode("utf-8")
        entries.append(
            {
                "name": name,
                "method": fields[4],
                "crc": fields[7],
                "csize": fields[8],
                "usize": fields[9],
                "offset": fields[16],
            }
        )
        cursor += 46 + name_len + extra_len + comment_len
    return entries


def _parse_central_directory(
    tail: bytes, tail_start: int
) -> tuple[int, int, int, list[dict[str, Any]]]:
    """从 zip 尾部解析中央目录：返回 (cd_offset, cd_size, n_total, entries)。"""
    pos = tail.rfind(b"PK\x05\x06")
    if pos < 0:
        raise FetchError("zip 尾部未找到 EOCD")
    _, _, _, _, n_total, cd_size, cd_off, _ = struct.unpack_from("<4s4H2LH", tail, pos)
    if cd_off + cd_size > tail_start + len(tail) or cd_off < tail_start:
        raise FetchError(
            f"中央目录不在尾部窗口内（cd_off={cd_off}, cd_size={cd_size}, tail_start={tail_start}）"
        )
    cd = tail[cd_off - tail_start : cd_off - tail_start + cd_size]
    if len(cd) != cd_size:
        raise FetchError("中央目录截断")
    return cd_off, cd_size, n_total, _parse_entries(cd, n_total)


def _extract_member(url: str, entry: dict[str, Any], dest: Path, *, token: str | None) -> None:
    """取单个成员：本地头 → 压缩数据 → 解压 → 大小/CRC/SHA 校验 → 落盘。"""
    offset = int(entry["offset"])
    header = _http_range(url, offset, offset + 29, token=token)
    if header[:4] != b"PK\x03\x04":
        raise FetchError(f"{entry['name']}: 本地头签名非法")
    name_len, extra_len = struct.unpack_from("<2H", header, 26)
    data_start = offset + 30 + name_len + extra_len
    compressed = _http_range(
        url, data_start, data_start + int(entry["csize"]) - 1, token=token
    )
    if entry["method"] == 8:
        payload = zlib.decompress(compressed, -15)
    else:
        payload = compressed
    if len(payload) != entry["usize"]:
        raise FetchError(f"{entry['name']}: 解压大小不符 {len(payload)} != {entry['usize']}")
    crc = binascii.crc32(payload) & 0xFFFFFFFF
    if crc != entry["crc"]:
        raise FetchError(f"{entry['name']}: CRC 不符 {crc:08x} != {entry['crc']:08x}")
    sha = hashlib.sha256(payload).hexdigest()
    expected_sha = str(entry["sha256"]).lower()
    if sha != expected_sha:
        raise FetchError(f"{entry['name']}: SHA-256 不符 {sha} != {expected_sha}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(payload)
    print(f"[mxc] {entry['name']} {len(payload)} bytes sha256={sha[:12]}…", flush=True)


def _verify_output(out: Path, manifest: dict[str, Any]) -> list[str]:
    """校验产物目录；返回问题列表（空 = 完整）。"""
    problems: list[str] = []
    for member in manifest["members"]:
        path = out / member["path"]
        if not path.is_file():
            problems.append(f"缺少 {member['path']}")
            continue
        data = path.read_bytes()
        if len(data) != member["size"]:
            problems.append(f"{member['path']} 大小不符")
            continue
        if hashlib.sha256(data).hexdigest() != member["sha256"]:
            problems.append(f"{member['path']} SHA-256 不符")
    return problems


def _write_manifest(out: Path, manifest: dict[str, Any]) -> None:
    payload = {
        "package": "mxc",
        "version": manifest["version"],
        "source": manifest["source"],
        "files": manifest["members"],
    }
    (out / "MXC_RUNTIME_MANIFEST.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _copy_license(out: Path, manifest: dict[str, Any]) -> None:
    source = REPO_ROOT / manifest["license"]["file"]
    if not source.is_file():
        raise FetchError(f"缺少许可证文件: {source}")
    shutil.copyfile(source, out / "LICENSE.md")


def fetch(
    manifest_path: Path, out: Path, *, force: bool, source_url: str | None, token: str | None
) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not force:
        problems = _verify_output(out, manifest)
        if not problems:
            print(f"[mxc] 产物已就绪且校验通过: {out}", flush=True)
            return 0
        print(f"[mxc] 产物不完整（{len(problems)} 项），重新取件", flush=True)

    url = source_url or manifest["source"]["api_url"]
    asset_size = int(manifest["source"]["size"])
    tail_start = max(0, asset_size - _TAIL_BYTES)
    tail = _http_range(url, tail_start, asset_size - 1, token=token)
    cd_off, cd_size, n_total, entries = _parse_central_directory(tail, tail_start)
    if cd_off < tail_start:
        # 中央目录比尾部窗口大：补取完整中央目录后单独解析条目
        cd = _http_range(url, cd_off, cd_off + cd_size - 1, token=token)
        entries = _parse_entries(cd, n_total)
    by_name = {entry["name"]: entry for entry in entries}

    staging = out.parent / f".{out.name}.tmp"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    for member in manifest["members"]:
        entry = by_name.get(member["path"])
        if entry is None:
            raise FetchError(f"发行包缺少成员: {member['path']}")
        if int(entry["usize"]) != int(member["size"]):
            raise FetchError(
                f"{member['path']}: 发行包成员大小与 pin 不符 "
                f"{entry['usize']} != {member['size']}"
            )
        _extract_member(
            url,
            {**entry, "sha256": member["sha256"]},
            staging / member["path"],
            token=token,
        )
    _write_manifest(staging, manifest)
    _copy_license(staging, manifest)
    problems = _verify_output(staging, manifest)
    if problems:
        raise FetchError("产物校验失败: " + "; ".join(problems))
    if out.exists():
        shutil.rmtree(out)
    staging.replace(out)
    print(f"[mxc] 完成: {out}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch MXC runtime binaries into build/")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--force", action="store_true", help="忽略已有产物强制重取")
    parser.add_argument("--check", action="store_true", help="只校验已有产物")
    parser.add_argument("--source-url", default=os.environ.get("MXC_SOURCE_URL"))
    args = parser.parse_args(argv)
    manifest_path = Path(args.manifest)
    out = Path(args.out)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if args.check:
        problems = _verify_output(out, manifest)
        if problems:
            print("[mxc] 校验失败: " + "; ".join(problems))
            return 1
        print(f"[mxc] 校验通过: {out}")
        return 0
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        return fetch(
            manifest_path,
            out,
            force=args.force,
            source_url=args.source_url,
            token=token,
        )
    except FetchError as exc:
        print(f"[mxc] 取件失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
