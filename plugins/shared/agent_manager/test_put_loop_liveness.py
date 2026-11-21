# @feature: FP-0.2.四 模式体系 P3-④ agent_manager 聚合注册表 | @ci: python-coverage
"""agent_manager 保存 agent 配置事件循环活性锁（light 合宿心跳不冻）。

事故根因（2026-11-12 异步阻塞审计 A-10）：``http_handle``（async）→
``put_agent_config`` → ``_put_mode_agent_config`` → ``_git_commit_user_copy``
同步 ``subprocess.run(git add/commit, timeout=8)`` ×2——最坏 16s 跑在 light
合宿宿主事件循环上（同宿主 20 成员连坐，watchdog 判停滞即整组退出）。

锁面（断行为）：
- 写回链（文件落盘 + 用户仓 git add/commit）整体出事件循环：假身 git 同步
  阻塞 0.25s/次，心跳间隔不得出现 ≥0.1s 空窗；假身线程 ≠ 事件循环线程；
- 语义不变：ETag 乐观锁、备份文件名、200 信封与 source=user_copy。

git 为外部依赖边界（真实子进程按假身替换）；文件 IO 一律真实临时目录。
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from _libs.testing.loop_liveness import MAX_TICK_GAP, LoopTicker

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

_MODULE_NAME = "agent_manager_put_loop_liveness"

_SYSTEM_YAML = "config_id: agentos\nname: 灵汐\nagent_type: main\nlevel: L1\n"
_SEED_YAML = (
    "config_id: code_writer\nname: 代码编写\ndescription: 模式包编码代理\n"
    "agent_type: specialized\nlevel: L2\napi_key: sk-seed-secret\n"
)


def _load_module() -> Any:
    """动态加载 server.py（独立模块名，避免与其他测试文件装载互相覆盖）。"""
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, str(_PLUGIN_DIR / "server.py"))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """临时三根：系统 config/agents + 出厂模式根 + 用户根（AGENTOS_USER_ROOT）。"""
    agents = tmp_path / "config" / "agents" / "main"
    agents.mkdir(parents=True)
    (agents / "agentos.yaml").write_text(_SYSTEM_YAML, encoding="utf-8")
    monkeypatch.setenv("AGENTOS_CONFIG_ROOT", str(tmp_path / "config"))
    monkeypatch.setenv("AGENTOS_USER_ROOT", str(tmp_path / "userroot"))
    module = _load_module()
    monkeypatch.setattr(module, "_factory_modes_dir", lambda: tmp_path / "factory_modes")
    return module


def _seed_factory_agent(tmp_path: Path) -> Path:
    seed = tmp_path / "factory_modes" / "mode_coding" / "agents" / "code_writer.yaml"
    seed.parent.mkdir(parents=True)
    seed.write_text(_SEED_YAML, encoding="utf-8")
    return seed


def _unwrap(resp: dict[str, Any]) -> dict[str, Any]:
    data = resp["data"]
    return {
        "success": resp.get("success"),
        "status": data["status"],
        "body": json.loads(base64.b64decode(data["body"])),
    }


# ============================================================
# 观测仪自检：心跳能看见同步阻塞（防测试空转假绿）
# ============================================================


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：同步 sleep 0.3s 必被 max_gap 捕获。"""
    async with LoopTicker() as ticker:
        time.sleep(0.3)
    assert ticker.max_gap >= 0.2


# ============================================================
# 主锁：保存 agent 配置（用户仓 git add/commit）出事件循环
# ============================================================


async def test_put_mode_agent_config_keeps_event_loop_ticking(
    server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模式包 agent 写回（ETag 基线 → 落盘 → git add/commit 2×0.25s）期间心跳
    持续打点；写面语义照常（副本内容、200 信封、source=user_copy）。"""
    seed = _seed_factory_agent(tmp_path)
    etag = hashlib.sha256(seed.read_text(encoding="utf-8").encode()).hexdigest()
    (tmp_path / "userroot" / ".git").mkdir(parents=True)

    threads: list[int] = []

    def _slow_git_run(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.get_ident())
        time.sleep(0.25)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(server.subprocess, "run", _slow_git_run)
    loop_thread = threading.get_ident()
    new_yaml = "config_id: code_writer\nname: 用户定制版\nlevel: L2\n"
    raw = json.dumps({"yaml": new_yaml, "if_match": etag})

    async with LoopTicker() as ticker:
        resp = await server.http_handle(
            path="/ext/agent_manager/agents/mode_coding/code_writer/config",
            method="PUT",
            raw_body=raw,
        )

    assert ticker.max_gap < MAX_TICK_GAP
    unwrapped = _unwrap(resp)
    assert unwrapped["status"] == 200, unwrapped["body"]
    assert unwrapped["body"]["source"] == "user_copy"
    assert unwrapped["body"]["backup"] is None, "首写建副本无备份"
    copy = tmp_path / "userroot" / "plugins" / "modes" / "mode_coding" / "agents" / "code_writer.yaml"
    assert copy.read_text(encoding="utf-8") == new_yaml
    assert seed.read_text(encoding="utf-8") == _SEED_YAML, "出厂种子必须原样保留"
    assert len(threads) == 2, "add + commit 各一次"
    assert all(tid != loop_thread for tid in threads), "git 子进程必须在事件循环线程之外"


async def test_put_mode_agent_config_stale_etag_409_without_git(
    server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """失败闭面不变：ETag 不匹配 409 且不触发 git 子进程。"""
    _seed_factory_agent(tmp_path)
    (tmp_path / "userroot" / ".git").mkdir(parents=True)
    calls: list[Any] = []
    def _record_run(*args: Any, **kwargs: Any) -> Any:
        del kwargs
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(server.subprocess, "run", _record_run)

    resp = await server.http_handle(
        path="/ext/agent_manager/agents/mode_coding/code_writer/config",
        method="PUT",
        raw_body=json.dumps({"yaml": "config_id: code_writer\n", "if_match": "stale"}),
    )

    unwrapped = _unwrap(resp)
    assert unwrapped["status"] == 409
    assert calls == []
