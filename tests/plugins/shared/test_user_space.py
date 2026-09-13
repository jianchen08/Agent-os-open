# @feature: FP-0.2.CFG user_space | @vision: V3 可嵌入 | @ci: python-coverage
"""用户空间解析测试：分区覆盖优先级 + OS 默认位置 + 空白视为未设 + 兜底。

与 ``kernel/crates/core/src/user_space.rs`` 同规则（共享真值源契约，与
``kernel_db.py`` ↔ ``storage_factory.rs`` 先例同构）——本文件锁 Python 侧，
Rust 侧由 ``user_space::tests`` 锁，两侧断言的行为必须一致。
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


# ── 分区默认位置（随用户根推导） ───────────────────────────────


def test_sub_roots_default_under_user_root(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(user_space.USER_ROOT_ENV, str(tmp_path))
    assert user_space.user_root() == tmp_path
    assert user_space.user_config_dir() == tmp_path / "config"
    assert user_space.user_data_dir() == tmp_path / "data"
    assert user_space.user_plugins_dir() == tmp_path / "plugins"


def test_root_env_overrides_os_default(
    clean_env: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # 两组区分度输入：显式根 vs 无显式根（后者必落 OS 标准目录下，不是 tmp）
    monkeypatch.setenv(user_space.USER_ROOT_ENV, str(tmp_path))
    assert user_space.user_root() == tmp_path

    monkeypatch.delenv(user_space.USER_ROOT_ENV)
    os_default = user_space.user_root()
    assert os_default is not None, "本平台应能推导 OS 标准数据目录"
    assert os_default.name == "agentos"
    assert os_default != tmp_path, "无显式根时不得等于显式根（证明分支真的切换了）"


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


# ── OS 默认目录语义 ──────────────────────────────────────────


def test_os_default_follows_platform_convention(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OS 默认位置按平台约定（与 dirs::data_dir() 对齐）。"""
    import sys as _sys

    if _sys.platform == "win32":
        monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
        assert user_space.user_root() == Path(r"C:\Users\x\AppData\Roaming") / "agentos"
        # 明示不用 %LOCALAPPDATA%：配置与密钥应随用户漫游
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
        assert "Local" not in str(user_space.user_root())
    elif _sys.platform == "darwin":
        assert user_space.user_root() == (
            Path.home() / "Library" / "Application Support" / "agentos"
        )
    else:
        monkeypatch.setenv("XDG_DATA_HOME", "/xdg/data")
        assert user_space.user_root() == Path("/xdg/data") / "agentos"
        # XDG 未设时回落 ~/.local/share
        monkeypatch.delenv("XDG_DATA_HOME")
        assert user_space.user_root() == Path.home() / ".local" / "share" / "agentos"


def test_xdg_blank_falls_back_to_local_share(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """XDG_DATA_HOME 空白视为未设（Linux 分支）——空白目录名会拼出相对路径。"""
    import sys as _sys

    if _sys.platform != "linux":
        pytest.skip("仅 Linux 分支涉及 XDG")

    monkeypatch.setenv("XDG_DATA_HOME", "   ")
    assert user_space.user_root() == Path.home() / ".local" / "share" / "agentos"


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
