# @feature: FP-0.2.CFG user_space | @vision: V3 可嵌入 | @ci: python-coverage
"""用户空间解析测试：分区覆盖优先级 + 零回落 + 空白视为未设。

与 ``kernel/crates/core/src/user_space.rs`` 同规则（共享真值源契约，与
``kernel_db.py`` ↔ ``storage_factory.rs`` 先例同构）——本文件锁 Python 侧，
Rust 侧由 ``user_space::tests`` 锁，两侧断言的行为必须一致。

**零回落**（ADR 2026-11-07-plugin-tiered-single-copy-model §2.2）：
``AGENTOS_USER_ROOT`` 未设/空白 = ``None``，不做 OS 标准目录推导。
2026-11-18 事故回归：Python 侧曾滞后保留 ``%APPDATA%\\agentos`` 回落，
dev 内核（未交付用户根）的 agent_config_load sidecar 经此读到**装机**用户层
的 executor 影子，工具面被陈旧副本接管（实测注入 28 件而非缩减后 12 件）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_ROOT = _REPO_ROOT / "plugins" / "shared"
if str(_SHARED_ROOT) not in sys.path:
    sys.path.insert(0, str(_SHARED_ROOT))

import user_space  # noqa: E402

from agentos_plugin_sdk import diagnostics_settings as ds  # noqa: E402

pytestmark = pytest.mark.unit

_ALL_ENV = (
    user_space.USER_ROOT_ENV,
    user_space.USER_CONFIG_DIR_ENV,
    user_space.USER_DATA_DIR_ENV,
    user_space.USER_PLUGINS_DIR_ENV,
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清空全部用户空间环境变量，杜绝宿主机真实取值渗入断言。"""
    for key in _ALL_ENV:
        monkeypatch.delenv(key, raising=False)


# ── SDK 包边界副本对齐（多端平台差异普查 ⚠️-3 收敛锚）──────────


class TestSdkDiagnosticsCopyAlignment:
    """SDK ``diagnostics_settings.user_root`` 副本与本体对齐断言。

    SDK 包边界不可 import user_space（user_space 是共享根裸模块，SDK 以
    site-packages 平拷贝布局装入各 venv 时无共享根可达）——副本合法存在，
    防漂移靠本组对齐断言；改动须与本体同刀（锚注释互链）。

    已裁定微差（非漂移，2026-11-18 重裁）：本体零回落（env 未设 = None），
    SDK 副本必须 ``never None``（settings.yaml 落位需要）——env 未设时副本
    保留 OS 兜底，env 显式时两侧同根。
    """

    @pytest.fixture(autouse=True)
    def _clear_user_root_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(user_space.USER_ROOT_ENV, raising=False)

    def test_env_override_same_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(user_space.USER_ROOT_ENV, str(tmp_path))
        assert ds.user_root() == user_space.user_root() == tmp_path

    def test_env_unset_both_degrade_per_ruling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """env 未设：本体 None（零回落）；SDK 副本保持 never-None（微差裁定）。"""
        assert user_space.user_root() is None
        sdk_root = ds.user_root()
        assert sdk_root is not None
        assert sdk_root.name == "agentos", "副本兜底仍锚 <OS 数据目录>/agentos"


# ── 分区默认位置（随用户根推导） ───────────────────────────────


def test_sub_roots_default_under_user_root(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(user_space.USER_ROOT_ENV, str(tmp_path))
    assert user_space.user_root() == tmp_path
    assert user_space.user_config_dir() == tmp_path / "config"
    assert user_space.user_data_dir() == tmp_path / "data"
    assert user_space.user_plugins_dir() == tmp_path / "plugins"


def test_root_env_switch_and_zero_fallback(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """两组区分度输入：显式根生效；未设 → None（零回落，绝不推导 OS 目录）。"""
    monkeypatch.setenv(user_space.USER_ROOT_ENV, str(tmp_path))
    assert user_space.user_root() == tmp_path

    monkeypatch.delenv(user_space.USER_ROOT_ENV)
    assert user_space.user_root() is None, "未设 = None（ADR 2026-11-07 §2.2 零回落）"


def test_accident_regression_installed_shadow_must_not_leak_into_dev(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """2026-11-18 事故回归：dev（用户根未交付）不得读装机用户层陈旧副本。

    构造 %APPDATA% 指向「装有 agent 影子的装机用户空间」；dev 内核不交付
    AGENTOS_USER_ROOT——四个分区根必须全 None，消费方（agent_config_load）
    因此回落 factory 配置，装机影子不可能接管 dev 工具面。
    """
    fake_appdata = tmp_path / "Roaming"
    shadow = fake_appdata / "agentos" / "config" / "agents" / "executor"
    shadow.mkdir(parents=True)
    (shadow / "general_agent.yaml").write_text("tool_ids: [stale_shadow]\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(fake_appdata))

    assert user_space.user_root() is None
    assert user_space.user_config_dir() is None, "dev 不得把装机用户配置层当自己的"
    assert user_space.user_data_dir() is None
    assert user_space.user_plugins_dir() is None


# ── 分区环境变量覆盖用户根 ────────────────────────────────────


def test_partition_env_overrides_root_derivation(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(user_space.USER_ROOT_ENV, str(tmp_path / "root"))
    elsewhere = tmp_path / "elsewhere"
    monkeypatch.setenv(user_space.USER_CONFIG_DIR_ENV, str(elsewhere))

    # 分区覆盖只影响本区，其余仍随用户根
    assert user_space.user_config_dir() == elsewhere
    assert user_space.user_data_dir() == tmp_path / "root" / "data"
    assert user_space.user_plugins_dir() == tmp_path / "root" / "plugins"


def test_data_dir_env_matches_legacy_name(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # AGENTOS_DATA_DIR 是既有变量（tenant_data.py 一直用它）——必须继续生效，
    # 否则存量的部署配置会在换默认值后静默指向别处
    monkeypatch.setenv(user_space.USER_ROOT_ENV, str(tmp_path / "root"))
    legacy = tmp_path / "legacy-data"
    monkeypatch.setenv(user_space.USER_DATA_DIR_ENV, str(legacy))
    assert user_space.user_data_dir() == legacy


# ── 空白值语义 ────────────────────────────────────────────────


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_env_treated_as_unset(
    blank: str, clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(user_space.USER_ROOT_ENV, str(tmp_path))
    monkeypatch.setenv(user_space.USER_CONFIG_DIR_ENV, blank)
    # 空白 ≠ 覆盖，应回退到用户根推导（与 Rust 侧 trim().is_empty() 一致）
    assert user_space.user_config_dir() == tmp_path / "config"


# ── 零回落（OS 目录推导已删除）────────────────────────────────


def test_os_env_vars_are_ignored_without_explicit_user_root(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APPDATA/XDG_DATA_HOME 存在也不参与推导——只认 AGENTOS_USER_ROOT。"""
    monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
    monkeypatch.setenv("XDG_DATA_HOME", "/xdg/data")
    assert user_space.user_root() is None


# ── 与 tenant_data 的接线 ────────────────────────────────────


def test_tenant_data_base_follows_user_space(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """tenant_data 的数据根跟随用户空间（默认值出仓的关键接线）。"""
    import tenant_data

    monkeypatch.setenv(user_space.USER_ROOT_ENV, str(tmp_path))
    monkeypatch.delenv(tenant_data.DATA_BASE_ENV, raising=False)
    # 经公共 API 推数据根：写一个子目录后它的父链应落在用户空间数据根下
    data_root = tenant_data.tenant_data_root("default", "probe", base=None)
    assert data_root.is_relative_to(tmp_path / "data"), (
        f"数据根应落在用户空间下，实际 {data_root}"
    )

    # ① 显式 AGENTOS_DATA_DIR 覆盖优先（既有语义不得回退）
    legacy = tmp_path / "legacy"
    monkeypatch.setenv(tenant_data.DATA_BASE_ENV, str(legacy))
    assert tenant_data.tenant_data_root("default", "probe", base=None).is_relative_to(
        legacy
    )


# ── payload_diag 快照目录单源（SDK，写端 adapter 与读端 monitoring 共用）──


class TestPayloadDiagDirAnchor:
    """``ds.payload_diag_dir``：LLM 请求快照目录读写单源。

    裁定 2026-11-02：快照锚用户空间 ``logs/payload_diag``，禁插件文件位置
    /cwd 推导——装机布局下写端探测命中包内 resources、读端 env 命中用户
    空间，读写分叉致「LLM 请求」调试页看不到新调用
    （ADR 2026-11-02-payload-diag-user-space-anchor）。
    """

    @pytest.fixture(autouse=True)
    def _clear_anchor_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("AGENTOS_LOG_DIR", raising=False)
        monkeypatch.delenv(user_space.USER_ROOT_ENV, raising=False)

    def test_log_dir_env_override_wins(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
        assert ds.payload_diag_dir() == tmp_path / "logs" / "payload_diag"

    def test_default_under_user_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv(user_space.USER_ROOT_ENV, str(tmp_path))
        assert ds.payload_diag_dir() == tmp_path / "logs" / "payload_diag"

    @pytest.mark.parametrize("blank", ["", "   ", "\t"])
    def test_blank_env_falls_back_to_user_root(
        self, blank: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 空白 env ≠ 覆盖（与 user_root 的空白语义一致）
        monkeypatch.setenv("AGENTOS_LOG_DIR", blank)
        monkeypatch.setenv(user_space.USER_ROOT_ENV, str(tmp_path))
        assert ds.payload_diag_dir() == tmp_path / "logs" / "payload_diag"

    def test_log_dir_override_beats_user_root(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 两组区分度输入：env 覆盖与用户根同时存在时，部署覆盖优先
        monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path / "deploy"))
        monkeypatch.setenv(user_space.USER_ROOT_ENV, str(tmp_path / "user"))
        assert ds.payload_diag_dir() == tmp_path / "deploy" / "logs" / "payload_diag"
