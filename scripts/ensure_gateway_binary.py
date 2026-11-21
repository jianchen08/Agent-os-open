#!/usr/bin/env python3
"""LLM 网关二进制填充配方：按版本锁定 + SHA-256 pin 从官方 release 下载
CLIProxyAPI 到插件 bin/（构建产物不入库，ADR 2026-10-16；打包链在出包前
调用本脚本填充，先例=build/mxc-runtime、app-mobile www 构建步骤）。

用法：
  python scripts/ensure_gateway_binary.py            # 下载/校验/落位（幂等）
  python scripts/ensure_gateway_binary.py --check    # 只校验已有产物（不下载）
  python scripts/ensure_gateway_binary.py --force    # 校验失败也重新下载覆盖

供应链纪律：
- 版本 + SHA-256 pin 常量锁定（官方 release checksums.txt 实证，2026-11-21）；
- 哈希不匹配 = 拒绝落位并退出非零（防供应链自造/传输损坏）；
- pin 两级：资产级（SHA256_PINS，release 压缩包，下载面权威闸）+ 成员级
  （解包后二进制，落位时写 bin/<成员名>.sha256 sidecar 记录）——--check/
  幂等路径对 sidecar 校验在盘产物，sidecar 缺席即 fail-visible 要求重跑
  下载路径（防在盘产物被替换/损坏而无从验证）；
- bin/ 整目录 gitignore——exe 永不入 git；
- 许可红线：bin/ 只落网关主二进制，provider dll 与 zcode-proxy 物理排除
  （许可三行结论见 docs/working/网关随包分发方案_20261121.md，不动）。

用法（打包链接线）：
  electron:build / release_all.sh 出包步骤内先行调用本脚本（网络不可达时
  打包失败 fail-visible，不带未校验二进制出门）。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import platform
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "plugins" / "shared" / "llm_provider_gateway"
BIN_DIR = PLUGIN_DIR / "bin"

# ── 版本锁定与 SHA-256 pin（官方 checksums.txt 实证，2026-11-21）──────────
# 来源：https://github.com/router-for-me/CLIProxyAPI/releases/download/
#       v8.0.80/checksums.txt （上游官方工件，MIT 二进制；windows_amd64 的
#       SHA-256 另经本机下载复算二次实证一致）
GATEWAY_VERSION = "8.0.80"
RELEASE_BASE = f"https://github.com/router-for-me/CLIProxyAPI/releases/download/v{GATEWAY_VERSION}"

# 平台键 → (release 资产名, 压缩包内二进制成员名, 期望 SHA-256)
SHA256_PINS: dict[str, tuple[str, str, str]] = {
    "windows-amd64": (
        f"CLIProxyAPI_{GATEWAY_VERSION}_windows_amd64.zip",
        "cli-proxy-api.exe",
        "13d3d9ef6528e5fcfee0d17195936a3332f542d565a96db3f33a9944a57b107f",
    ),
    "windows-aarch64": (
        f"CLIProxyAPI_{GATEWAY_VERSION}_windows_aarch64.zip",
        "cli-proxy-api.exe",
        "d12ed70cf9a9709b5a0d1f8b9affdb5e0252a3387e1bd842e4eeef0744c54bb6",
    ),
    "linux-amd64": (
        f"CLIProxyAPI_{GATEWAY_VERSION}_linux_amd64.tar.gz",
        "cli-proxy-api",
        "74ea0252f18f12332d10709d6f196558c72c56429265f05142ab241749a61c16",
    ),
    "linux-aarch64": (
        f"CLIProxyAPI_{GATEWAY_VERSION}_linux_aarch64.tar.gz",
        "cli-proxy-api",
        "c016b9238858fb01d0e720331ed8962df88fa7b635cad9bc5707b857fe82ec5a",
    ),
    "darwin-amd64": (
        f"CLIProxyAPI_{GATEWAY_VERSION}_darwin_amd64.tar.gz",
        "cli-proxy-api",
        "764f28c6b8271f8ea3e606f32e71ae5a4442a97ddf6fc23ca452f93d274624c3",
    ),
    "darwin-aarch64": (
        f"CLIProxyAPI_{GATEWAY_VERSION}_darwin_aarch64.tar.gz",
        "cli-proxy-api",
        "c22e3b15a0d4395e779dc70b6e4ea78bdf3053c93c06fe9a15e96161d4ed46d3",
    ),
}

# 许可红线：这些名字/模式禁止出现在 bin/（物理排除，非注释性约束）
FORBIDDEN_IN_BIN = ("*.dll", "zcode-proxy*")


def platform_key() -> str:
    """当前平台 → pin 表键（machine 归一 amd64/aarch64）。"""
    system = platform.system().strip().lower()
    machine = platform.machine().strip().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "amd64"
    if system.startswith("win"):
        return f"windows-{arch}"
    if system.startswith("darwin") or system.startswith("mac"):
        return f"darwin-{arch}"
    return f"linux-{arch}"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def existing_binary(platform_id: str) -> Path | None:
    """bin/ 下已落位的规范二进制（平台规范名），存在即返回路径。"""
    _, member, _ = SHA256_PINS[platform_id]
    path = BIN_DIR / member
    return path if path.is_file() else None


def existing_binary_matches(platform_id: str) -> bool:
    """在盘产物完整性：对落位 sidecar（bin/<成员名>.sha256）校验成员哈希。

    sidecar 由 download_and_install 落位时写入（资产 pin 通过后才有 sidecar）；
    sidecar 缺席 = 在盘产物无完整性记录（手工拷入或历史落位），不可验证即
    False（fail-visible，走重下载路径重建对账依据）。"""
    _, member, _ = SHA256_PINS[platform_id]
    sidecar = BIN_DIR / f"{member}.sha256"
    if not sidecar.is_file():
        return False
    expected = sidecar.read_text(encoding="utf-8").split()[0].strip().lower()
    actual = sha256_of((BIN_DIR / member).read_bytes())
    return actual == expected


def download_and_install(platform_id: str) -> Path:
    """下载 release 资产 → 校验 SHA-256 pin → 解包二进制到 bin/。

    哈希不匹配即失败（exit 非），绝不落未校验文件。
    """
    asset, member, expected_sha = SHA256_PINS[platform_id]
    url = f"{RELEASE_BASE}/{asset}"
    print(f"[ensure_gateway_binary] 下载 {url}")
    with urllib.request.urlopen(url, timeout=600) as response:  # noqa: S310 —— 固定 https 官方域
        blob = response.read()
    actual_sha = sha256_of(blob)
    if actual_sha != expected_sha:
        raise SystemExit(
            f"[ensure_gateway_binary] SHA-256 不匹配，拒绝落位（供应链防护）：\n"
            f"  期望 {expected_sha}\n  实得 {actual_sha}\n"
            f"  资产 {asset}——请核实官方 checksums.txt 后更新 pin 再试"
        )
    print(f"[ensure_gateway_binary] SHA-256 校验通过: {actual_sha}")
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    target = BIN_DIR / member
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = zf.namelist()
            if member not in names:
                raise SystemExit(f"[ensure_gateway_binary] 压缩包缺成员 {member}（含: {names[:10]}...）")
            source = zf.read(member)
    else:
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            member_handle = tf.extractfile(member)
            if member_handle is None:
                raise SystemExit(f"[ensure_gateway_binary] 压缩包缺成员 {member}")
            source = member_handle.read()
    # 临时文件 + 原子替换（半写文件不落位）
    with tempfile.NamedTemporaryFile(dir=str(BIN_DIR), delete=False) as tmp:
        tmp.write(source)
        tmp_path = Path(tmp.name)
    os_replace(tmp_path, target)
    if not asset.endswith(".zip"):
        target.chmod(0o755)  # POSIX 可执行位
    # 成员级完整性记录（--check/幂等路径的对账依据；sidecar 随 bin/ gitignore）
    member_sha = sha256_of(source)
    sidecar = BIN_DIR / f"{member}.sha256"
    sidecar.write_text(f"{member_sha}  {member}\n", encoding="utf-8")
    print(f"[ensure_gateway_binary] 已落位 {target}（{target.stat().st_size / 1e6:.1f} MB，成员 SHA-256 {member_sha[:12]}…）")
    return target


def os_replace(src: Path, dst: Path) -> None:
    dst.unlink(missing_ok=True)
    shutil.move(str(src), str(dst))


def check_forbidden_files() -> None:
    """许可红线核查：bin/ 内出现 dll / zcode-proxy 即失败。"""
    if not BIN_DIR.is_dir():
        return
    offenders: list[Path] = []
    for pattern in FORBIDDEN_IN_BIN:
        offenders.extend(BIN_DIR.glob(pattern))
    if offenders:
        raise SystemExit(f"[ensure_gateway_binary] 许可红线：bin/ 内出现禁随包产物 {offenders}")


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM 网关二进制填充配方（版本锁定+SHA-256 pin）")
    parser.add_argument("--check", action="store_true", help="只校验已有产物（不下载）")
    parser.add_argument("--force", action="store_true", help="已有产物校验失败时重新下载覆盖")
    args = parser.parse_args()

    platform_id = platform_key()
    existing = existing_binary(platform_id)
    if existing is not None:
        if args.check:
            if not existing_binary_matches(platform_id):
                print(
                    "[ensure_gateway_binary] 在盘产物成员哈希不匹配或完整性记录缺席"
                    "——重跑本脚本（下载路径）重建对账依据"
                )
                return 1
            print(f"[ensure_gateway_binary] 产物校验通过: {existing}")
            check_forbidden_files()
            return 0
        if args.force or not existing_binary_matches(platform_id):
            print("[ensure_gateway_binary] 现有产物缺失/损坏/无完整性记录/强制刷新——重新下载")
        else:
            print(f"[ensure_gateway_binary] 已就位且校验通过: {existing}")
            check_forbidden_files()
            return 0
    elif args.check:
        print(f"[ensure_gateway_binary] bin/ 无产物（{BIN_DIR}）")
        return 1
    download_and_install(platform_id)
    check_forbidden_files()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
