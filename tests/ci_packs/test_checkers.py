# @feature: FP-0.2.〇 项目类型 CI 包 | @ci: python-coverage
"""base 六 checker 行为测试（设计稿 §3.1/§7/§9.2/§9.9）。

真实 checker 件 + 临时 git 仓造文件；staged 清单经 stdin 注入（hook 同构）。
退出码契约：0=绿 / 1=违规（fail-closed）/ 2=skip（fail-open，如规范规则缺席）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from tests.ci_packs.conftest import PACKS_ROOT, git, run_checker

pytestmark = pytest.mark.unit

BASE_CHECKS = PACKS_ROOT / "base" / "checks"


@pytest.fixture
def ci_project(git_project: Path) -> Path:
    """带真实 base checks 的临时 git 项目。"""
    ci = git_project / ".agentos" / "ci"
    (ci / "checks").mkdir(parents=True)
    for py in BASE_CHECKS.glob("*.py"):
        shutil.copyfile(py, ci / "checks" / py.name)
    return git_project


def _stage(project: Path, files: dict[str, str | bytes]) -> None:
    for rel, content in files.items():
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8") if isinstance(content, str) else content
        p.write_bytes(data)
    git(project, "add", "-A")


class TestNoBuildArtifacts:
    def test_staged_pyc_block(self, ci_project: Path) -> None:
        _stage(ci_project, {"app/__pycache__/m.pyc": "x"})
        r = run_checker(ci_project, "no_build_artifacts.py", stdin=b"app/__pycache__/m.pyc\n")
        assert r.returncode == 1
        assert "m.pyc" in r.stdout

    def test_staged_node_modules_block(self, ci_project: Path) -> None:
        _stage(ci_project, {"web/node_modules/pkg/index.js": "x"})
        r = run_checker(ci_project, "no_build_artifacts.py", stdin=b"web/node_modules/pkg/index.js\n")
        assert r.returncode == 1

    def test_clean_source_passes(self, ci_project: Path) -> None:
        _stage(ci_project, {"src/main.py": "print(1)\n", "docs/a.md": "# t\n"})
        r = run_checker(ci_project, "no_build_artifacts.py", stdin=b"src/main.py\ndocs/a.md\n")
        assert r.returncode == 0

    def test_tracked_fallback_without_stdin(self, ci_project: Path) -> None:
        """无 staged 清单（手动直跑）→ 回落 git ls-files 全仓扫描。"""
        _stage(ci_project, {"build/out.o": "x"})
        git(ci_project, "commit", "-m", "init")
        r = run_checker(ci_project, "no_build_artifacts.py")
        assert r.returncode == 1
        assert "out.o" in r.stdout


class TestOversizedFiles:
    def test_oversize_staged_block(self, ci_project: Path) -> None:
        _stage(ci_project, {"assets/big.bin": b"0" * (10 * 1024 * 1024 + 1)})
        r = run_checker(ci_project, "oversized_files.py", stdin=b"assets/big.bin\n")
        assert r.returncode == 1
        assert "big.bin" in r.stdout

    def test_small_file_passes(self, ci_project: Path) -> None:
        _stage(ci_project, {"assets/small.bin": b"0" * 1024})
        r = run_checker(ci_project, "oversized_files.py", stdin=b"assets/small.bin\n")
        assert r.returncode == 0

    def test_max_bytes_override(self, ci_project: Path) -> None:
        """run argv 声明式槽位：--max-bytes 覆盖出厂默认。"""
        _stage(ci_project, {"a.bin": b"0" * 2048, "b.bin": b"0" * 4096})
        r = run_checker(ci_project, "oversized_files.py", "--max-bytes", "3000", stdin=b"a.bin\nb.bin\n")
        assert r.returncode == 1
        assert "b.bin" in r.stdout and "a.bin" not in r.stdout


class TestRepoSizeBudget:
    def test_over_budget_blocks(self, ci_project: Path) -> None:
        _stage(ci_project, {"data/a.bin": b"0" * 9000, "data/b.bin": b"0" * 8000})
        git(ci_project, "commit", "-m", "init")
        r = run_checker(ci_project, "repo_size_budget.py", "--budget-bytes", "1000")
        assert r.returncode == 1
        assert "a.bin" in r.stdout  # 列大头清单

    def test_within_budget_passes(self, ci_project: Path) -> None:
        _stage(ci_project, {"data/a.bin": b"0" * 9000})
        git(ci_project, "commit", "-m", "init")
        r = run_checker(ci_project, "repo_size_budget.py", "--budget-bytes", "50000")
        assert r.returncode == 0

    def test_default_budget_trivially_green_for_small_repo(self, ci_project: Path) -> None:
        _stage(ci_project, {"a.txt": "x"})
        git(ci_project, "commit", "-m", "init")
        assert run_checker(ci_project, "repo_size_budget.py").returncode == 0


class TestDocLinks:
    def test_broken_relative_link_blocks(self, ci_project: Path) -> None:
        _stage(ci_project, {"README.md": "[x](docs/missing.md)\n"})
        r = run_checker(ci_project, "doc_links.py", stdin=b"README.md\n")
        assert r.returncode == 1
        assert "docs/missing.md" in r.stdout

    def test_broken_image_block(self, ci_project: Path) -> None:
        _stage(ci_project, {"README.md": "![pic](img/gone.png)\n"})
        r = run_checker(ci_project, "doc_links.py", stdin=b"README.md\n")
        assert r.returncode == 1

    def test_valid_links_pass(self, ci_project: Path) -> None:
        _stage(
            ci_project,
            {
                "README.md": "[a](docs/ok.md) ![i](img/ok.png) [web](https://example.com) [锚](#sec)\n",
                "docs/ok.md": "# ok\n",
                "img/ok.png": "png",
            },
        )
        r = run_checker(ci_project, "doc_links.py", stdin=b"README.md\ndocs/ok.md\nimg/ok.png\n")
        assert r.returncode == 0

    def test_relative_resolution_is_per_file(self, ci_project: Path) -> None:
        """相对链接以所在 md 为基准（docs/b.md 引 ../README.md 应解析成功）。"""
        _stage(ci_project, {"README.md": "# r\n", "docs/b.md": "[up](../README.md)\n"})
        r = run_checker(ci_project, "doc_links.py", stdin=b"docs/b.md\nREADME.md\n")
        assert r.returncode == 0


class TestSecretsScan:
    def test_aws_key_block(self, ci_project: Path) -> None:
        key = "AKIA" + "ABCDEFGHIJKLMNOP"
        _stage(ci_project, {"conf/app.env": f"aws_id={key}\n"})
        r = run_checker(ci_project, "secrets_scan.py", stdin=b"conf/app.env\n")
        assert r.returncode == 1
        assert "app.env" in r.stdout

    def test_private_key_block(self, ci_project: Path) -> None:
        _stage(ci_project, {"cert.pem": "-----BEGIN RSA " + "PRIVATE KEY-----\nabc\n"})
        r = run_checker(ci_project, "secrets_scan.py", stdin=b"cert.pem\n")
        assert r.returncode == 1

    def test_clean_file_passes(self, ci_project: Path) -> None:
        _stage(ci_project, {"src/a.py": "password = os.environ['PW']\ntoken = config['token']\n"})
        r = run_checker(ci_project, "secrets_scan.py", stdin=b"src/a.py\n")
        assert r.returncode == 0


class TestAssetConventions:
    """J4 素材规范引擎：出厂出引擎不出规则——规则在 local/asset_rules.json。"""

    _RULES = {"rules": [{"match": ["*.wav", "*.ogg"], "allow_dirs": ["assets/audio"], "note": "音频只准入 assets/audio/"}]}

    def test_no_rules_file_skips(self, ci_project: Path) -> None:
        _stage(ci_project, {"s.wav": "x"})
        r = run_checker(ci_project, "asset_conventions.py", stdin=b"s.wav\n")
        assert r.returncode == 2
        assert "asset_rules" in r.stdout

    def test_violation_reports(self, ci_project: Path) -> None:
        local = ci_project / ".agentos" / "ci" / "local"
        local.mkdir(parents=True)
        (local / "asset_rules.json").write_text(json.dumps(self._RULES), encoding="utf-8")
        _stage(ci_project, {"s.wav": "x", "keep/other.png": "x"})
        r = run_checker(ci_project, "asset_conventions.py", stdin=b"s.wav\nkeep/other.png\n")
        assert r.returncode == 1
        assert "s.wav" in r.stdout

    def test_compliant_placement_passes(self, ci_project: Path) -> None:
        local = ci_project / ".agentos" / "ci" / "local"
        local.mkdir(parents=True)
        (local / "asset_rules.json").write_text(json.dumps(self._RULES), encoding="utf-8")
        _stage(ci_project, {"assets/audio/s.wav": "x"})
        r = run_checker(ci_project, "asset_conventions.py", stdin=b"assets/audio/s.wav\n")
        assert r.returncode == 0

    def test_invalid_rules_file_fails_closed(self, ci_project: Path) -> None:
        """规则文件本身写坏 = 红（fail-closed），不是 skip。"""
        local = ci_project / ".agentos" / "ci" / "local"
        local.mkdir(parents=True)
        (local / "asset_rules.json").write_text("{{not json", encoding="utf-8")
        _stage(ci_project, {"s.wav": "x"})
        r = run_checker(ci_project, "asset_conventions.py", stdin=b"s.wav\n")
        assert r.returncode == 1
