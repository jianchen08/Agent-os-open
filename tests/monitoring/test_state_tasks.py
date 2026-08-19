"""monitoring /ext/monitoring/tasks state 派生 + payload_diag 目录锚定测试。

2026-08-19 调试中心修复：
- /ext/monitoring/tasks 原硬编码空列表 → 从全部管道 state（pipeline-state.list，
  内存热 + DB 冷兜底）派生，不过滤状态，status 过滤仅由 query 决定；
- payload_diag 目录锚定：AGENTOS_LOG_DIR 优先，否则从文件位置向上探测项目根
  （原 cwd 漂移曾导致读写两端错位——快照散落在插件目录）。
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit  # 0.2 TDD 分层：单元测试

# 按路径直接加载 monitoring/server.py 为独立模块名——裸名 ``import server`` 依赖
# sys.path 顺序，会被先收集的其它插件目录（channel_api 也有 server.py）抢先解析
# （pytest 会话启动即导入全部 conftest，目录插入顺序不保证 monitoring 在前）。
# conftest 仍负责把 monitoring 目录加进 sys.path（server.py 内部的平铺导入需要）。
_SERVER_PY = (
    Path(__file__).resolve().parents[2] / "plugins" / "shared" / "system" / "monitoring" / "server.py"
)
_spec = importlib.util.spec_from_file_location("monitoring_server_under_test", _SERVER_PY)
monitoring_server = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(monitoring_server)


class _FakeCap:
    """pipeline-state.list 假能力句柄。"""

    def __init__(self, rows):
        self._rows = rows

    async def call(self, method, params):
        assert (method, params) == ("list", {})
        return self._rows


class TestStateTasks:
    """_collect_state_tasks 行为。"""

    ROWS = [
        {
            "pipeline_id": "pipeA", "thread_id": "th-a", "agent_id": "agentos",
            "task.id": "t-1", "task.goal": "写贪吃蛇", "task.status": "completed",
            "current_phase": "exit", "ended": True, "message_count": 12,
            "track.total_tokens": 3400,
        },
        {
            "pipeline_id": "pipeB", "thread_id": "th-b",
            "status": "running", "current_phase": "tool", "display_name": "灵汐",
            "message_count": 3,
        },
        {"pipeline_id": None, "status": "running"},  # 无 pipeline_id 的行应被跳过
    ]

    async def test_derives_items_from_all_states(self, monkeypatch):
        monkeypatch.setattr(
            monitoring_server.plugin, "get_capability",
            lambda _name: _FakeCap(self.ROWS),
        )
        items = await monitoring_server._collect_state_tasks()
        assert [i["pipeline_id"] for i in items] == ["pipeA", "pipeB"]
        first = items[0]
        assert first["id"] == "t-1"  # task.id 优先
        assert first["title"] == "写贪吃蛇"
        assert first["status"] == "completed"
        assert first["total_tokens"] == 3400
        assert first["source"] == "pipeline_state"
        # 无 task.goal → display_name 兜底
        assert items[1]["title"] == "灵汐"
        assert items[1]["id"] == "pipeB"  # 无 task.id → pipeline_id

    async def test_status_filter_applied_only_when_given(self, monkeypatch):
        monkeypatch.setattr(
            monitoring_server.plugin, "get_capability",
            lambda _name: _FakeCap(self.ROWS),
        )
        assert len(await monitoring_server._collect_state_tasks()) == 2  # 无过滤：全部状态
        only = await monitoring_server._collect_state_tasks(status="completed")
        assert [i["pipeline_id"] for i in only] == ["pipeA"]

    async def test_capability_failure_degrades_to_empty(self, monkeypatch):
        def _boom(_name):
            raise KeyError("capability not injected")

        monkeypatch.setattr(monitoring_server.plugin, "get_capability", _boom)
        assert await monitoring_server._collect_state_tasks() == []


class TestPayloadDiagDirAnchor:
    """payload_diag 目录锚定（读端与写端同源）。"""

    def test_env_var_takes_priority(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AGENTOS_LOG_DIR", str(tmp_path))
        assert monitoring_server._payload_diag_dir() == os.path.join(
            tmp_path, "logs", "payload_diag"
        )

    def test_walk_up_to_project_root_when_env_missing(self, monkeypatch):
        monkeypatch.delenv("AGENTOS_LOG_DIR", raising=False)
        resolved = monitoring_server._payload_diag_dir()
        # 必须落在探测到的项目根（含 config/models），而非插件目录 cwd
        root = monitoring_server._resolve_project_root()
        assert os.path.isdir(os.path.join(root, "config", "models"))
        assert resolved == os.path.join(root, "logs", "payload_diag")
