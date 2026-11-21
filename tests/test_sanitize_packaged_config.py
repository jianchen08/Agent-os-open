# @feature: FP-0.2.PACK 打包/部署 CI 门 | @ci: python-coverage
"""桌面打包 config 净化门禁（scripts/sanitize_packaged_config.mjs）契约测试。

electron:build 链内联闸（package.json electron:build 步），三路 fail-closed：
- 开发机绝对路径/.bak 残渣（原 2026-09-27 R295 面）；
- 出厂画像字段实填（profile.md 个人信息字段非占位即拦，2026-11-18 处置）；
- config 文本面公网 IPv4（可路由真值地址即拦；与 deploy/sanitize_seed_config.py
  第四路同律，禁两处漂移）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT = _REPO_ROOT / "scripts" / "sanitize_packaged_config.mjs"

pytestmark = pytest.mark.unit

_PLACEHOLDER_PROFILE = """# 用户画像

## 基本信息
- 用户名：默认用户
- 出生年份：（用户空间自填）
- 性别：（用户空间自填）
"""


def _run_gate(cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # 退出码即被测契约，check=False 显式声明
        ["node", str(_SCRIPT)],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )


def _mk_config(root: Path) -> Path:
    cfg = root / "config"
    cfg.mkdir(parents=True)
    return cfg


class TestDevPathFace:
    def test_dev_absolute_path_blocks(self, tmp_path: Path) -> None:
        _mk_config(tmp_path).joinpath("a.yaml").write_text(
            'root: "D:\\myproject\\container_x"\n', encoding="utf-8"
        )
        result = _run_gate(tmp_path)
        assert result.returncode == 1
        assert "a.yaml" in result.stderr

    def test_bak_residue_blocks(self, tmp_path: Path) -> None:
        _mk_config(tmp_path).joinpath("b.yaml.bak").write_text("x: 1\n", encoding="utf-8")
        assert _run_gate(tmp_path).returncode == 1


class TestProfileFieldFace:
    def test_filled_personal_field_blocks(self, tmp_path: Path) -> None:
        users = _mk_config(tmp_path) / "users" / "default"
        users.mkdir(parents=True)
        (users / "profile.md").write_text(
            _PLACEHOLDER_PROFILE.replace("- 性别：（用户空间自填）", "- 性别：示例值"),
            encoding="utf-8",
        )
        result = _run_gate(tmp_path)
        assert result.returncode == 1
        assert "profile.md" in result.stderr

    def test_placeholder_profile_passes(self, tmp_path: Path) -> None:
        users = _mk_config(tmp_path) / "users" / "default"
        users.mkdir(parents=True)
        (users / "profile.md").write_text(_PLACEHOLDER_PROFILE, encoding="utf-8")
        assert _run_gate(tmp_path).returncode == 0


class TestPublicIpv4Face:
    def test_public_ip_blocks(self, tmp_path: Path) -> None:
        _mk_config(tmp_path).joinpath("net.yaml").write_text(
            "ingestion_url: http://62.1.2.3/e\n", encoding="utf-8"
        )
        result = _run_gate(tmp_path)
        assert result.returncode == 1
        assert "62.1.2.3" in result.stderr

    def test_reserved_and_private_ips_pass(self, tmp_path: Path) -> None:
        _mk_config(tmp_path).joinpath("net.yaml").write_text(
            "a: 127.0.0.1\nb: 0.0.0.0\nc: 172.20.0.1\nd: 203.0.113.153\ne: 192.0.2.147\n",
            encoding="utf-8",
        )
        assert _run_gate(tmp_path).returncode == 0

    def test_non_text_files_not_scanned(self, tmp_path: Path) -> None:
        _mk_config(tmp_path).joinpath("blob.bin").write_bytes(b"http://62.1.2.3/\x00\xff")
        assert _run_gate(tmp_path).returncode == 0


class TestCleanTree:
    def test_clean_config_passes(self, tmp_path: Path) -> None:
        _mk_config(tmp_path).joinpath("ok.yaml").write_text("k: v\n", encoding="utf-8")
        result = _run_gate(tmp_path)
        assert result.returncode == 0
        assert "净化检查通过" in result.stdout
