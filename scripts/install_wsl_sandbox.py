#!/usr/bin/env python3
"""把 wsl_native 沙箱供给装进 WSL 发行版（免 sudo、免 apt 安装）。

装两件东西到发行版用户目录 `<HOME>/.agentos/bin/`：
- `bwrap`：从发行版 apt 源 `apt-get download bubblewrap` 取包后 `dpkg-deb -x`
  解出二进制（不需要 root，也不改发行版包状态）；
- `aos-sandbox`：仓内 `plugins/shared/isolation/scripts/aos-sandbox.sh` 的副本。

设计真值：docs/decisions/2026-11-10-wsl-native-workspace-only-sandbox.md
装完不影响正在运行的任务：`sandbox_cmd` 仍是出厂空值，启用是
`isolation_config.yaml` 里的一行配置（见 ADR「影响 · 上线口径」）。

用法：
    python scripts/install_wsl_sandbox.py                 # 安装 + 自检
    python scripts/install_wsl_sandbox.py --distro Ubuntu
    python scripts/install_wsl_sandbox.py --check-only    # 只自检，不装

退出码：0 成功；2 wsl/发行版不可用；3 bwrap 取证失败；4 自检失败。
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WRAPPER = REPO_ROOT / "plugins" / "shared" / "isolation" / "scripts" / "aos-sandbox.sh"

# 取 bwrap：apt-get download 不落包状态、不需要 root（本机实测可行，
# 候选版本随发行版仓库走，不钉死版本号——升级由 apt 源自然跟随）。
_BWRAP_SCRIPT = r"""
set -e
bin_dir="$HOME/.agentos/bin"
mkdir -p "$bin_dir"
if "$bin_dir/bwrap" --version >/dev/null 2>&1; then
  echo "bwrap: $("$bin_dir/bwrap" --version)（已就位）"
  exit 0
fi
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
cd "$tmp"
if ! apt-get download bubblewrap >/dev/null 2>&1; then
  echo "apt-get download bubblewrap 失败（发行版仓库不可达或包名不同）" >&2
  exit 3
fi
deb="$(ls bubblewrap_*.deb 2>/dev/null | head -1)"
[ -n "$deb" ] || { echo "未取到 bubblewrap 包" >&2; exit 3; }
dpkg-deb -x "$deb" ./root
install -m 0755 ./root/usr/bin/bwrap "$bin_dir/bwrap"
"$bin_dir/bwrap" --version
"""

_CHECK_SCRIPT = r"""
set -e
bin="$HOME/.agentos/bin"
"$bin/bwrap" --version
[ -x "$bin/aos-sandbox" ] || { echo "aos-sandbox 未安装" >&2; exit 4; }
cd "${TMPDIR:-/tmp}"
echo "--- 沙箱 profile（dry-run）---"
AOS_SANDBOX_DRY_RUN=1 "$bin/aos-sandbox" bash -c true
echo "--- 沙箱内实测 ---"
"$bin/aos-sandbox" bash -c 'echo "ws=$(pwd)"; echo "/mnt -> $(ls /mnt 2>&1)"; echo "写测试 -> $(touch .aos_probe 2>&1 && echo OK)"; rm -f .aos_probe'
"""


def _run_wsl(distro: str, script: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """在发行版内跑一段 bash（脚本经 argv 传入，不经 shell 重解析宿主侧内容）。"""
    return subprocess.run(
        ["wsl.exe", "-d", distro, "--exec", "bash", "-c", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _fail(code: int, proc: subprocess.CompletedProcess[str]) -> int:
    sys.stderr.write((proc.stderr or proc.stdout or "").strip() + "\n")
    return code


def _install_wrapper(distro: str, wrapper: Path) -> int:
    """把仓内包装脚本写进发行版用户目录（base64 传输，避免引号/换行改写）。"""
    payload = base64.b64encode(wrapper.read_bytes()).decode("ascii")
    script = (
        "set -e\n"
        'bin="$HOME/.agentos/bin"\n'
        'mkdir -p "$bin"\n'
        f'printf %s {payload} | base64 -d > "$bin/aos-sandbox"\n'
        'chmod 0755 "$bin/aos-sandbox"\n'
        'echo "aos-sandbox: $bin/aos-sandbox"\n'
    )
    proc = _run_wsl(distro, script, timeout=120)
    if proc.returncode != 0:
        return _fail(4, proc)
    print(proc.stdout.strip())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="安装 wsl_native bwrap 沙箱供给")
    parser.add_argument("--distro", default="Ubuntu", help="WSL 发行版名（默认 Ubuntu）")
    parser.add_argument(
        "--wrapper",
        default=str(DEFAULT_WRAPPER),
        help="包装脚本源文件（默认仓内 plugins/shared/isolation/scripts/aos-sandbox.sh）",
    )
    parser.add_argument("--check-only", action="store_true", help="只自检，不安装")
    args = parser.parse_args()

    wrapper = Path(args.wrapper)
    if not args.check_only and not wrapper.is_file():
        sys.stderr.write(f"包装脚本不存在: {wrapper}\n")
        return 2

    probe = _run_wsl(args.distro, "echo ok", timeout=60)
    if probe.returncode != 0 or "ok" not in (probe.stdout or ""):
        sys.stderr.write(f"WSL 发行版不可用: {args.distro}\n{probe.stderr or ''}")
        return 2

    if not args.check_only:
        proc = _run_wsl(args.distro, _BWRAP_SCRIPT)
        if proc.returncode != 0:
            return _fail(3, proc)
        print(proc.stdout.strip())

        code = _install_wrapper(args.distro, wrapper)
        if code != 0:
            return code

    proc = _run_wsl(args.distro, _CHECK_SCRIPT, timeout=120)
    if proc.returncode != 0:
        return _fail(4, proc)
    print(proc.stdout.strip())
    print("\n启用方式：config/plugins/isolation/isolation_config.yaml 的")
    print("wsl_native.sandbox_cmd 改为：")
    print("  sandbox_cmd:")
    print("  - bash")
    print("  - -c")
    print('  - exec "$HOME/.agentos/bin/aos-sandbox" "$@"')
    print("  - --")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
