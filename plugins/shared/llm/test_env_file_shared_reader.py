# @feature: FP-0.2.CFG 配置读写单源化 | @ci: python-test
""".env 读取单源端到端测试（B3 双读取器归一）。

设置页写 key 的真实落点 = 用户空间 .env（内核 ``PUT /api/v1/config/env`` 写面
恒落用户空间，与启动加载、spawn 叠加同源 ``env_path_for_root``）。本文件锁定
「写侧落点 → 读侧解析」全链同值：

- 运行时解析面（``_config_models.set_config`` → ``${VAR}`` 展开）读到设置页
  写入的值——归一前运行时兜底只读项目根 .env，用户空间 key 解析不到（字面量
  401 实害）；
- 展示面（``routes_llm_config._resolve_env_value``）与运行时面同源同值；
- 优先级：进程环境 > 用户空间 .env > 项目根 .env（进程环境最高是内核
  env_delta_overlay 的「系统环境 > .env」语义，文件兜底不得翻转）；
- 解析器与内核写面引号语义对齐（``write_env_updates`` 对含空白/# 的值加双
  引号，读侧剥引号还原）；
- 机械对账闸：两消费点必须同一函数（双读取器不回潮）。

外部依赖：文件系统（tmp_path 模拟用户空间/项目根落点）；不触网。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_DIR = Path(__file__).resolve().parent
_SHARED_DIR = _DIR.parents[0]
for _p in (str(_DIR), str(_SHARED_DIR)):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _config_models as ccm  # noqa: E402
import routes_llm_config as rlc  # noqa: E402


@pytest.fixture
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """隔离三方落点：用户空间/项目根均指 tmp，模块级缓存与配置清零。"""
    user_root = tmp_path / "user_root"
    project_root = tmp_path / "project"
    user_root.mkdir(parents=True)
    project_root.mkdir(parents=True)
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(user_root))
    monkeypatch.setattr(ccm, "_resolve_project_root", lambda: project_root)
    monkeypatch.setattr(ccm, "_config", {})
    monkeypatch.setattr(ccm, "_env_cache", None)
    return {"user": user_root, "project": project_root}


def test_settings_page_key_reaches_runtime_resolution(
    isolated: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """设置页写 key（用户空间 .env）→ 运行时解析面读到同值。

    只写用户空间、项目根无 .env——归一前运行时兜底只读项目根，此 key 解析
    不到（占位符字面量发上游 → 401 实害路径）。
    """
    monkeypatch.delenv("B3_SETTLE_KEY", raising=False)
    (isolated["user"] / ".env").write_text("B3_SETTLE_KEY=user-space-value\n", encoding="utf-8")
    config = {"llm": {"providers": {"p": {"api_key": "${B3_SETTLE_KEY}"}}}}

    ccm.set_config(config)

    assert ccm.get_config()["llm"]["providers"]["p"]["api_key"] == "user-space-value"
    # 性质断言：解析幂等——同配置重复注入结果一致（不依赖调用次序/缓存状态）
    ccm.set_config(config)
    assert ccm.get_config()["llm"]["providers"]["p"]["api_key"] == "user-space-value"


def test_user_space_key_wins_over_project_root(
    isolated: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """同键双源：用户空间 .env 优先于项目根 .env（与内核写面/展示面同视角）。"""
    monkeypatch.delenv("B3_BOTH_KEY", raising=False)
    (isolated["project"] / ".env").write_text("B3_BOTH_KEY=from-project\n", encoding="utf-8")
    (isolated["user"] / ".env").write_text("B3_BOTH_KEY=from-user\n", encoding="utf-8")

    ccm.set_config({"llm": {"providers": {"p": {"api_key": "${B3_BOTH_KEY}"}}}})

    assert ccm.get_config()["llm"]["providers"]["p"]["api_key"] == "from-user"


def test_quoted_value_from_kernel_write_face_roundtrips(
    isolated: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """内核 write_env_updates 对含 #/空白值加双引号——读侧剥引号还原原值。"""
    monkeypatch.delenv("B3_QUOTED_KEY", raising=False)
    (isolated["user"] / ".env").write_text(
        'B3_QUOTED_KEY="secret with #hash"\n', encoding="utf-8"
    )

    ccm.set_config({"llm": {"providers": {"p": {"api_key": "${B3_QUOTED_KEY}"}}}})

    assert ccm.get_config()["llm"]["providers"]["p"]["api_key"] == "secret with #hash"


def test_process_env_still_beats_file(
    isolated: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """进程环境仍最高（内核 env_delta_overlay 注入语义不被 .env 文件翻转）。"""
    monkeypatch.setenv("B3_ENV_KEY", "from-process")
    (isolated["user"] / ".env").write_text("B3_ENV_KEY=from-file\n", encoding="utf-8")

    ccm.set_config({"llm": {"providers": {"p": {"api_key": "${B3_ENV_KEY}"}}}})

    assert ccm.get_config()["llm"]["providers"]["p"]["api_key"] == "from-process"


def test_display_face_reads_same_file_as_runtime(
    isolated: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """展示面（routes._resolve_env_value）与运行时解析面同源同值。"""
    monkeypatch.delenv("B3_VIEW_KEY", raising=False)
    (isolated["user"] / ".env").write_text("B3_VIEW_KEY=shared-value\n", encoding="utf-8")

    assert rlc._resolve_env_value("${B3_VIEW_KEY}") == "shared-value"

    ccm.set_config({"llm": {"providers": {"p": {"api_key": "${B3_VIEW_KEY}"}}}})
    assert ccm.get_config()["llm"]["providers"]["p"]["api_key"] == "shared-value"


def test_key_rotation_picked_up_after_rewrite(
    isolated: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """key 轮换（.env 重写）→ 缓存失效读到新值（指纹热重启语义的读侧半边）。"""
    monkeypatch.delenv("B3_ROTATE_KEY", raising=False)
    user_env = isolated["user"] / ".env"
    user_env.write_text("B3_ROTATE_KEY=old\n", encoding="utf-8")
    ccm.set_config({"llm": {"providers": {"p": {"api_key": "${B3_ROTATE_KEY}"}}}})
    assert ccm.get_config()["llm"]["providers"]["p"]["api_key"] == "old"

    user_env.write_text("B3_ROTATE_KEY=new\n", encoding="utf-8")
    ccm.set_config({"llm": {"providers": {"p": {"api_key": "${B3_ROTATE_KEY}"}}}})
    assert ccm.get_config()["llm"]["providers"]["p"]["api_key"] == "new"


def test_env_file_path_without_user_space_module_falls_back(
    isolated: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    """user_space 共享模块不可得（仅挂插件目录的加载形态）→ 回落项目根 .env。"""
    monkeypatch.setitem(sys.modules, "user_space", None)
    monkeypatch.delenv("B3_NOUS_KEY", raising=False)
    (isolated["project"] / ".env").write_text("B3_NOUS_KEY=from-project\n", encoding="utf-8")

    assert ccm.env_file_path() == isolated["project"] / ".env"
    assert ccm.env_file_vars() == {"B3_NOUS_KEY": "from-project"}


def test_env_reader_is_single_sourced() -> None:
    """机械对账闸：routes 的 .env 读取器必须实现于 _config_models（单源不回潮）。

    比源文件而非函数对象同一——test_config_injection 用 importlib.reload
    重置 _config_models 模块级状态（对象重建、源文件不变），比对象会把
    合法的重载误判成双源。
    """
    import inspect

    for reader in (rlc.env_file_path, rlc.env_file_vars):
        source = inspect.getsourcefile(reader)
        assert source is not None
        assert Path(source).name == "_config_models.py"
