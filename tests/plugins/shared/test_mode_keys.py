# @feature: FP-0.2.〇 模式体系测试补标 | @ci: python-coverage
# @feature: 模式体系P3 mode_coding试点 | @ci: python-coverage
"""mode_keys 平铺模块行为测试：模式命名空间 agent 键解析（§2.3/§3.3 两级解析第二级）。

断输入→输出（键形 → (mode, stem)/路径/None），不 mock 文件面：
出厂命中走真实 mode_coding 包；用户副本优先/双根回落经 AGENTOS_USER_ROOT
钉到 tmp 验证（与 plugins/shared/user_space.py 同一解析契约）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from mode_keys import (
    find_mode_agent_yaml,
    find_mode_package_dir,
    parse_mode_agent_key,
)

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_user_root(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """出厂命中断言不得依赖机器真实用户根（播种副本随生命周期演进）。

    AGENTOS_USER_ROOT 钉到空 tmp = 「删副本回落出厂」场景；用户副本优先已在
    各用户根用例中显式构造。
    """
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path_factory.mktemp("user-root")))


class TestParseModeAgentKey:
    @pytest.mark.parametrize(
        "key,expected",
        [
            ("mode_coding/code_writer", ("coding", "code_writer")),
            ("mode_writing/chapter_writer", ("writing", "chapter_writer")),
            ("mode_a/x", ("a", "x")),
        ],
    )
    def test_valid_keys(self, key: str, expected: tuple[str, str]) -> None:
        assert parse_mode_agent_key(key) == expected

    @pytest.mark.parametrize(
        "key",
        [
            "code_writer",              # 裸键（系统命名空间）
            "mode_coding",              # 无 stem 段
            "coding/code_writer",       # 缺 mode_ 前缀
            "mode_/x",                  # 空 mode 名
            "mode_Coding/x",            # mode 名含大写
            "mode_0coding/x",           # mode 名数字开头
            "mode_coding/../secret",    # stem 路径穿越
            "mode_coding/a-b",          # stem 含连字符
            "mode_coding/x/y",          # 多级段
            "",                         # 空键
        ],
    )
    def test_invalid_keys_return_none(self, key: str) -> None:
        assert parse_mode_agent_key(key) is None


class TestFindModePackageDir:
    def test_factory_package_hit(self) -> None:
        """出厂种子真实命中：mode_coding 包目录含 plugin.json。"""
        pkg = find_mode_package_dir("coding")
        assert pkg is not None
        assert pkg.name == "mode_coding"
        assert (pkg / "plugin.json").is_file()

    def test_missing_package_returns_none(self) -> None:
        assert find_mode_package_dir("no_such_mode") is None

    def test_user_copy_wins_over_factory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """双根同 id 用户赢：用户副本存在时优先返回。"""
        user_pkg = tmp_path / "plugins" / "modes" / "mode_coding"
        (user_pkg / "agents").mkdir(parents=True)
        (user_pkg / "plugin.json").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))

        pkg = find_mode_package_dir("coding")
        assert pkg == user_pkg


class TestFindModeAgentYaml:
    @pytest.mark.parametrize(
        "stem",
        [
            "programming_orchestrator_agent_v2",
            "code_writer",
            "code_reviewer_agent",
            "test_debug_agent",
        ],
    )
    def test_factory_agent_hit(self, stem: str) -> None:
        """出厂 mode_coding 包内四个迁入 agent 的键全部可解析（试点迁移验收）。"""
        path = find_mode_agent_yaml(f"mode_coding/{stem}")
        assert path is not None
        assert path.name == f"{stem}.yaml"
        assert path.parent == Path(__file__).resolve().parents[3].joinpath(
            "plugins", "shared", "modes", "mode_coding", "agents"
        )

    @pytest.mark.parametrize("stem", ["godot_expert", "godot_orchestrator_agent"])
    def test_factory_agent_hit_godot_package(self, stem: str) -> None:
        """出厂 mode_godot 包内迁入 agent 的键可解析（P3-⑤ godot 立项验收）。"""
        path = find_mode_agent_yaml(f"mode_godot/{stem}")
        assert path is not None
        assert path.name == f"{stem}.yaml"
        assert path.parent == Path(__file__).resolve().parents[3].joinpath(
            "plugins", "shared", "modes", "mode_godot", "agents"
        )

    def test_user_copy_agent_hit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用户副本 agents/<stem>.yaml 命中（键按文件名 stem 解析）。"""
        user_pkg = tmp_path / "plugins" / "modes" / "mode_coding"
        (user_pkg / "agents").mkdir(parents=True)
        (user_pkg / "plugin.json").write_text("{}", encoding="utf-8")
        user_yaml = user_pkg / "agents" / "code_writer.yaml"
        user_yaml.write_text("display_name: 用户版\n", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))

        assert find_mode_agent_yaml("mode_coding/code_writer") == user_yaml

    def test_user_agent_without_package_marker_not_resolved(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """用户目录缺 plugin.json 包标记 → 不算模式包（出厂也无此包 → None）。"""
        user_pkg = tmp_path / "plugins" / "modes" / "mode_ghostmode" / "agents"
        user_pkg.mkdir(parents=True)
        (user_pkg / "code_writer.yaml").write_text("display_name: 用户版\n", encoding="utf-8")
        monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path))

        assert find_mode_agent_yaml("mode_ghostmode/code_writer") is None

    @pytest.mark.parametrize(
        "key",
        [
            "mode_coding/no_such_stem",   # 包存在但 stem 不存在
            "mode_no_such_mode/any",      # 包不存在
            "code_writer",                # 裸键不进模式包解析
        ],
    )
    def test_miss_returns_none(self, key: str) -> None:
        assert find_mode_agent_yaml(key) is None
