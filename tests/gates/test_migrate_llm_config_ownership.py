# @feature: FP-0.2.CFG 配置读写单源化 | @ci: python-test
"""migrate_llm_config_ownership.py 纯函数单测（2026-09-28 方案批次 D）。

行为契约（断输入→输出，不钉实现）：
- deep_merge_config：factory 独有 models/providers 条目补入；同名冲突用户赢；
  defaults 按 policy 取侧（factory=缺陷期内 UI 保存落点的最新意图）；factory
  独有顶层键补入；两侧相同时 report 如实记 0。
- parse_env_text / merge_env：只增不改——factory 独有变量追加尾部，用户已有
  变量的值与顺序原样保留；注释与空行不参与；无缺漏时原文返回。
- has_ownership：账本路径精确匹配。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

pytestmark = pytest.mark.unit


def _load():
    spec = importlib.util.spec_from_file_location("migrate_llm_config_ownership", SCRIPTS / "migrate_llm_config_ownership.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


def _user_data() -> dict:
    return {
        "concurrency": {"default_max_concurrent": 2},
        "defaults": {"chat": "minimax-m3", "tiers": {"large": "minimax-m3"}},
        "models": {
            "minimax-m3": {"provider": "minimax", "model_name": "MiniMax-M3"},
            "webchat-deepseek": {"provider": "webchat", "model_name": "webchat-deepseek"},
        },
        "providers": {"minimax": {"api_base": "https://api.minimaxi.com/v1"}},
    }


def _factory_data() -> dict:
    return {
        "concurrency": {"default_max_concurrent": 4},
        "defaults": {"chat": "MiniMax-M3.1-Flash-Preview", "tiers": {"large": "MiniMax-M3.1-Flash-Preview"}},
        "models": {
            "minimax-m3": {"provider": "minimax", "model_name": "MiniMax-M3"},
            "MiniMax-M3.1-Flash-Preview": {"provider": "minimax", "model_name": "MiniMax-M3.1-Flash-Preview"},
        },
        "providers": {"minimax": {"api_base": "https://api.minimaxi.com/v1"}},
    }


class TestDeepMergeConfig:
    def test_factory_only_entries_merged_user_wins_conflicts(self) -> None:
        merged, report = mod.deep_merge_config(_user_data(), _factory_data(), "factory")

        # factory 独有模型并入（本机漂移实况：M3.1 在 factory）
        assert "MiniMax-M3.1-Flash-Preview" in merged["models"]
        # 用户独有条目保留（webchat-deepseek）
        assert "webchat-deepseek" in merged["models"]
        assert "minimax-m3" in merged["models"]
        assert report["factory_only_models"] == ["MiniMax-M3.1-Flash-Preview"]
        # 同名同值不算冲突（无漂移）
        assert report["conflict_models_user_won"] == []
        # 顶层冲突用户赢（concurrency 用户保持 2）
        assert merged["concurrency"] == {"default_max_concurrent": 2}
        assert "providers" in merged

    def test_conflicting_values_reported_user_wins(self) -> None:
        user = _user_data()
        factory = _factory_data()
        # 同名但内容不同 → 记冲突、用户值保留
        factory["models"]["webchat-deepseek"] = {"provider": "webchat", "model_name": "changed"}
        merged, report = mod.deep_merge_config(user, factory, "factory")
        assert report["conflict_models_user_won"] == ["webchat-deepseek"]
        assert merged["models"]["webchat-deepseek"]["model_name"] == "webchat-deepseek"

    def test_defaults_policy_factory_takes_newest_ui_intent(self) -> None:
        merged, report = mod.deep_merge_config(_user_data(), _factory_data(), "factory")
        assert merged["defaults"]["chat"] == "MiniMax-M3.1-Flash-Preview"
        assert report["defaults_from"] == "factory"

    def test_defaults_policy_user_keeps_user_choice(self) -> None:
        merged, report = mod.deep_merge_config(_user_data(), _factory_data(), "user")
        assert merged["defaults"]["chat"] == "minimax-m3"
        assert report["defaults_from"] == "user"

    def test_identical_sides_report_zero(self) -> None:
        data = _factory_data()
        merged, report = mod.deep_merge_config(data, data, "factory")
        assert merged == data
        assert report["factory_only_models"] == []
        assert report["conflict_models_user_won"] == []
        assert report["factory_only_top_keys"] == []


class TestEnvMerge:
    def test_missing_vars_appended_existing_untouched(self) -> None:
        factory_text = "A=1\nB=2\nC=3\n"
        user_text = "# my env\nB=kept\n"
        merged, added = mod.merge_env(factory_text, user_text)

        assert added == ["A", "C"]
        assert "B=kept" in merged  # 用户值不被覆盖
        assert "B=2" not in merged
        assert merged.index("B=kept") < merged.index("A=1")  # 追加在尾部
        assert "A=1" in merged and "C=3" in merged

    def test_no_missing_returns_original(self) -> None:
        factory_text = "A=1\n"
        user_text = "A=x\nB=y\n"
        merged, added = mod.merge_env(factory_text, user_text)
        assert merged == user_text
        assert added == []

    def test_comments_and_blank_lines_skipped(self) -> None:
        factory_text = "# comment\n\nA=1\n"
        user_text = "B=2\n"
        merged, added = mod.merge_env(factory_text, user_text)
        assert added == ["A"]
        assert "# comment" not in merged


class TestHasOwnership:
    def test_path_exact_match(self) -> None:
        ledger = [{"path": "plugins/llm/llm.yaml"}, {"path": "kernel/default_profile.yaml"}]
        assert mod.has_ownership(ledger, "plugins/llm/llm.yaml")
        assert not mod.has_ownership(ledger, "plugins/llm/missing.yaml")
        assert not mod.has_ownership([], "plugins/llm/llm.yaml")
