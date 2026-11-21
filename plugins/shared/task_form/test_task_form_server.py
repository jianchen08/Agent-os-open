# @feature: FP-0.2.二 任务表单插件服务 | @vision: V1 可进化 | @ci: none-local
"""任务表单服务（task_form）TDD 测试。

验证内容：
1. GET /ext/task_form/form 返回 manifest 声明路径
   （config/plugins/task_form/task_form.yaml）里的字段（前端 fieldsUri 消费）；
   声明文件缺席 → 空字段表 + 告警（读面不崩）
2. GET /ext/task_form/options/agents 返回 {value:config_id,label:name}（含嵌套子目录）
3. GET /ext/task_form/options/projects 返回登记行选项（共享 project_registry）
4. 登记簿真空（可读、无项目）→ 空列表且无提示；登记簿故障（模块缺失/实例化
   失败）→ 空列表 + warning 提示字段（故障与真空可区分）
5. 未匹配 path → 404

唯一外部依赖是临时 config 目录（AGENTOS_PROJECT_ROOT 指向），不接真实内核/服务。
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import logging
import sys
import time
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


@pytest.fixture
def form_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """临时项目根：config/plugins/task_form/task_form.yaml + config/agents/**/*.yaml。

    声明文件落点与 manifest ``config_files[0].path`` 一致（唯一真相源）。
    """
    config_dir = tmp_path / "config"
    form_dir = config_dir / "plugins" / "task_form"
    agents_main = config_dir / "agents" / "main"
    agents_exec = config_dir / "agents" / "executor"
    for d in (form_dir, config_dir / "agents", agents_main, agents_exec):
        d.mkdir(parents=True, exist_ok=True)
    (form_dir / "task_form.yaml").write_text(
        """
form:
  id: task_create
  title: 新建任务
  fields:
    - { name: title, type: input, label: 标题, required: true }
    - { name: target_id, type: select, label: 执行 Agent, datasourceUri: /ext/task_form/options/agents }
    - { name: project_id, type: select, label: 挂靠项目, datasourceUri: /ext/task_form/options/projects }
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
    # 登记簿落在临时 tasks 数据根（隔离真实登记目录）
    monkeypatch.setenv("TASKS_STORAGE_DIR", str(tmp_path / "tasks_data"))
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
    assert names == ["title", "target_id", "project_id", "workspace"]
    # datasourceUri 随字段声明带出（前端据此自内核取数）
    target = next(f for f in d["payload"]["fields"] if f["name"] == "target_id")
    assert target["datasourceUri"] == "/ext/task_form/options/agents"
    project = next(f for f in d["payload"]["fields"] if f["name"] == "project_id")
    assert project["datasourceUri"] == "/ext/task_form/options/projects"


async def test_form_missing_declaration_returns_empty_with_warning(
    form_project: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """声明文件缺席 → 空字段表 + 告警留痕（读面不崩，不缺省伪造字段）。"""
    (form_project["config"] / "plugins" / "task_form" / "task_form.yaml").unlink()
    mod = _load_module()
    with caplog.at_level(logging.WARNING, logger=mod.logger.name):
        res = await mod.http_handle(path="/ext/task_form/form", method="GET", query={})
    d = _resp_data(res)
    assert d["status"] == 200
    assert d["payload"]["fields"] == []
    assert "表单声明读取失败" in caplog.text
    assert "task_form.yaml" in caplog.text


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


def _stub_project_capability(mod: Any, items: list[dict[str, Any]] | Exception) -> None:
    """project capability 句柄桩（list 按预设应答；Exception = 服务故障形态）。"""

    class _Handle:
        async def call(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
            assert method == "list"
            if isinstance(items, Exception):
                raise items
            return {"items": items, "total": len(items)}

    mod.plugin.get_capability = lambda name: _Handle()  # type: ignore[method-assign]


async def test_projects_options_from_registry(form_project: dict[str, Any]) -> None:
    """项目选项 = project.list 登记行（{value:id,label:title}）。"""
    mod = _load_module()
    _stub_project_capability(mod, [{"id": "abc123def456", "title": "项目甲"}])

    res = await mod.http_handle(path="/ext/task_form/options/projects", method="GET", query={})
    d = _resp_data(res)
    assert d["status"] == 200
    data = d["payload"]["data"]
    assert len(data) == 1
    assert data[0]["label"] == "项目甲"
    assert data[0]["value"] == "abc123def456"


async def test_projects_options_empty_registry(form_project: dict[str, Any]) -> None:
    """服务可用但真空 → 空列表且无提示字段（真空 ≠ 故障）。"""
    mod = _load_module()
    _stub_project_capability(mod, [])
    res = await mod.http_handle(path="/ext/task_form/options/projects", method="GET", query={})
    d = _resp_data(res)
    assert d["status"] == 200
    assert d["payload"]["data"] == []
    assert "warning" not in d["payload"]


async def test_projects_options_service_down_warns(
    form_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """project.list 服务不可用 → 空列表 + warning 提示字段（故障不伪装成无项目）。"""
    mod = _load_module()
    _stub_project_capability(mod, RuntimeError("project_manager sidecar down"))
    res = await mod.http_handle(path="/ext/task_form/options/projects", method="GET", query={})
    d = _resp_data(res)
    assert d["status"] == 200
    assert d["payload"]["data"] == []
    warning = d["payload"].get("warning")
    assert isinstance(warning, str)
    assert "project.list" in warning


async def test_projects_options_bad_payload_warns(
    form_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """服务返回载荷非法（缺 items）→ 同样落 warning 提示字段（与真空区分）。"""
    mod = _load_module()
    _stub_project_capability(mod, [{"unexpected": True}])
    # 直接把应答换成非 list items 的畸形 dict
    class _BadHandle:
        async def call(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
            return {"no_items": True}

    mod.plugin.get_capability = lambda name: _BadHandle()  # type: ignore[method-assign]
    res = await mod.http_handle(path="/ext/task_form/options/projects", method="GET", query={})
    d = _resp_data(res)
    assert d["status"] == 200
    assert d["payload"]["data"] == []
    warning = d["payload"].get("warning")
    assert isinstance(warning, str)
    assert "project.list" in warning


async def test_unknown_path_404(form_project: dict[str, Any]) -> None:
    mod = _load_module()
    res = await mod.http_handle(path="/ext/task_form/nope", method="GET", query={})
    assert res["success"] is True
    payload = json.loads(base64.b64decode(res["data"]["body"]))
    assert res["data"]["status"] == 404
    assert "not found" in payload.get("error", "")


# ── 事件循环活性：执行 Agent 选项取数不冻合宿心跳 ──────────────────
#
# 审计 P2-14 同族：``http_handle`` 是 async handler（合宿 light 组同宿主连坐），
# ``_agent_options()`` 是同步全量扫描——``config/agents/**`` 逐文件 yaml.safe_load，
# 量级随配置树增长。锁面 = 取数段出事件循环（心跳 tick 间隔不出现 ≥0.1s 空窗）。

_MAX_TICK_GAP = 0.1
_BLOCK_SECS = 0.3


class _Ticker:
    """心跳协程：记录 tick 间隔 + 被测段收尾时刻（末段计入，防整段停滞漏判）。"""

    def __init__(self, interval: float = 0.01) -> None:
        self._interval = interval
        self._ticks: list[float] = []
        self._stop: asyncio.Event | None = None
        self._task: asyncio.Task[None] | None = None
        self._end = 0.0

    async def __aenter__(self) -> _Ticker:
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._run())
        await asyncio.sleep(self._interval * 3)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        assert self._stop is not None
        assert self._task is not None
        self._end = time.monotonic()
        self._stop.set()
        await self._task

    async def _run(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            self._ticks.append(time.monotonic())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
            except TimeoutError:
                continue

    @property
    def max_gap(self) -> float:
        points = [*self._ticks, self._end]
        return max(
            (b - a for a, b in zip(points, points[1:], strict=False)), default=0.0
        )


async def test_ticker_detects_blocking_call() -> None:
    """仪器的区分度自证：协程内同步 sleep 必被 max_gap 捕获。"""
    async with _Ticker() as ticker:
        time.sleep(_BLOCK_SECS)
    assert ticker.max_gap >= 0.2


async def test_agents_options_scan_keeps_event_loop_ticking(
    form_project: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置树全量解析（0.3s 量级）期间心跳持续打点；选项载荷照旧。"""
    mod = _load_module()
    real = mod._agent_options
    calls: list[int] = []

    def _slow_options() -> list[dict[str, Any]]:
        calls.append(1)
        time.sleep(_BLOCK_SECS)
        return real()

    monkeypatch.setattr(mod, "_agent_options", _slow_options)

    async with _Ticker() as ticker:
        res = await mod.http_handle(
            path="/ext/task_form/options/agents", method="GET", query={}
        )

    assert ticker.max_gap < _MAX_TICK_GAP, (
        f"选项取数冻结事件循环（期间心跳最大空窗 {ticker.max_gap:.3f}s）"
    )
    assert calls == [1]
    d = _resp_data(res)
    assert d["status"] == 200
    assert {"value": "agentos", "label": "灵汐"} in d["payload"]["data"]
    labels = [o["label"] for o in d["payload"]["data"]]
    assert labels == sorted(labels)
