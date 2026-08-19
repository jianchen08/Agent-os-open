# @feature: 任务表单插件服务 | @vision: V1 可进化 | @ci: none-local
"""任务表单服务（task_form）TDD 测试。

验证内容：
1. GET /ext/task_form/form 返回 config/task_form.yaml 声明的字段（前端 fieldsUri 消费）
2. 传 session_id 时容器选项 datasourceUri 内嵌 session（值驱动取数的前置）
3. GET /ext/task_form/options/agents 返回 {value:config_id,label:name}（含嵌套子目录）
4. GET /ext/task_form/options/containers 服务不可用时降级空列表（读面不崩）
5. 未匹配 path → 404

唯一外部依赖是临时 config 目录（AGENTOS_PROJECT_ROOT 指向），不接真实内核/服务。
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_PLUGIN_DIR = Path(__file__).resolve().parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))


def _load_module() -> Any:
    """动态加载 server.py（每次新建，隔离模块级状态）。"""
    spec = importlib.util.spec_from_file_location(
        "task_form_server_test",
        str(_PLUGIN_DIR / "server.py"),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["task_form_server_test"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def form_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """临时项目根：config/task_form.yaml + config/agents/**/*.yaml。"""
    config_dir = tmp_path / "config"
    agents_main = config_dir / "agents" / "main"
    agents_exec = config_dir / "agents" / "executor"
    for d in (config_dir / "agents", agents_main, agents_exec):
        d.mkdir(parents=True, exist_ok=True)
    (config_dir / "task_form.yaml").write_text(
        """
form:
  id: task_create
  title: 新建任务
  fields:
    - { name: title, type: input, label: 标题, required: true }
    - { name: target_id, type: select, label: 执行 Agent, datasourceUri: /ext/task_form/options/agents, dependsOn: [task_scope] }
    - { name: parent_task_id, type: select, label: 父容器, datasourceUri: /ext/task_form/options/containers }
    - { name: workspace, type: input, label: 工作空间 }
""",
        encoding="utf-8",
    )
    (agents_main / "agentos.yaml").write_text(
        "config_id: agentos\nname: 灵汐\n",
        encoding="utf-8",
    )
    (agents_exec / "general_agent.yaml").write_text(
        "config_id: general_agent\nname: 通用任务执行者\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTOS_PROJECT_ROOT", str(tmp_path))
    return {"root": tmp_path, "config": config_dir}


def _resp_data(result: dict[str, Any]) -> dict[str, Any]:
    """解 base64 body，返回 http handle 的 data 里的 JSON。"""
    assert result["success"] is True
    enc = result["data"]
    payload = json.loads(base64.b64decode(enc["body"]))
    return {"status": enc["status"], "payload": payload}


async def test_form_returns_declared_fields(form_project: dict[str, Any]) -> None:
    mod = _load_module()
    res = await mod.http_handle(path="/ext/task_form/form", method="GET", query={})
    d = _resp_data(res)
    assert d["status"] == 200
    names = [f["name"] for f in d["payload"]["fields"]]
    assert names == ["title", "target_id", "parent_task_id", "workspace"]
    # datasourceUri 随字段声明带出（前端据此自内核取数）
    target = next(f for f in d["payload"]["fields"] if f["name"] == "target_id")
    assert target["datasourceUri"] == "/ext/task_form/options/agents"
    assert target["dependsOn"] == ["task_scope"]
    # 未传 session → 容器 datasourceUri 原样
    parent = next(f for f in d["payload"]["fields"] if f["name"] == "parent_task_id")
    assert parent["datasourceUri"] == "/ext/task_form/options/containers"


async def test_form_embeds_session_into_container_datasource(form_project: dict[str, Any]) -> None:
    mod = _load_module()
    res = await mod.http_handle(
        path="/ext/task_form/form", method="GET", query={"session_id": "sess_abc"}
    )
    d = _resp_data(res)
    parent = next(f for f in d["payload"]["fields"] if f["name"] == "parent_task_id")
    assert parent["datasourceUri"] == "/ext/task_form/options/containers?session_id=sess_abc"
    # 其它字段不受影响
    target = next(f for f in d["payload"]["fields"] if f["name"] == "target_id")
    assert "session_id" not in target["datasourceUri"]


async def test_agents_options_shape(form_project: dict[str, Any]) -> None:
    mod = _load_module()
    res = await mod.http_handle(path="/ext/task_form/options/agents", method="GET", query={})
    d = _resp_data(res)
    assert d["status"] == 200
    data = d["payload"]["data"]
    # 含嵌套子目录（main/executor），value=config_id、label=name
    assert {"value": "agentos", "label": "灵汐"} in data
    assert {"value": "general_agent", "label": "通用任务执行者"} in data
    # 按 label 排序
    labels = [o["label"] for o in data]
    assert labels == sorted(labels)


async def test_containers_options_degrades_gracefully(form_project: dict[str, Any]) -> None:
    # 测试环境无 task_service 服务 → 降级可空列表，不抛错
    mod = _load_module()
    res = await mod.http_handle(
        path="/ext/task_form/options/containers", method="GET", query={"session_id": "sess_abc"}
    )
    d = _resp_data(res)
    assert d["status"] == 200
    assert isinstance(d["payload"]["data"], list)


async def test_unknown_path_404(form_project: dict[str, Any]) -> None:
    mod = _load_module()
    res = await mod.http_handle(path="/ext/task_form/nope", method="GET", query={})
    assert res["success"] is True
    payload = json.loads(base64.b64decode(res["data"]["body"]))
    assert res["data"]["status"] == 404
    assert "not found" in payload.get("error", "")
