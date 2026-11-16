# @feature: FP-0.2.CFG 配置直读闸判定面 | @ci: python-coverage
"""配置直读闸判定面回归：真违规形状必中，非路径 token 不误伤。

背景：分段形态原正则把测试里给 ``git config`` 设身份的
``"config", "user.email"`` 判成 config/ 直读（基线 0 下即假红）。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_scripts = str(ROOT / "scripts")
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import check_config_direct_reads as cdr  # noqa: E402


class TestCountViolations:
    def test_slash_form_hit(self):
        assert cdr.count_violations('Path("config/agents/main.yaml")') == 1

    def test_segment_form_with_path_hit(self):
        assert cdr.count_violations('Path("config", "agents/main.yaml")') == 1

    def test_bare_dir_segment_hit(self):
        # 无点的裸段按目录名收（"config" 与 "x.yaml" 相邻时第二段才有后缀）
        assert cdr.count_violations('Path("config", "agents")') == 1

    def test_path_join_form_hit(self):
        assert cdr.count_violations('Path("config" / "storage.yaml")') == 1

    def test_git_identity_not_hit(self):
        # 测试给 git 设身份的命令不是 config/ 直读
        assert cdr.count_violations('git("config", "user.email")') == 0
        assert cdr.count_violations('_git(root, "config", "user.name")') == 0

    def test_dotted_non_config_segment_not_hit(self):
        assert cdr.count_violations('run("config", "x.txt")') == 0
