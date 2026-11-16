#!/usr/bin/env python3
"""mode_personal MCP 服务端——pim 工具 + REST http.handle + pim.query 服务。

合宿 light 组契约：模块级 plugin 实例 + 装载期完成注册（CohostServer 聚合
plugin._tools，注册留独立运行入口则合宿下成员零工具）。数据域单例经
get_store()/set_store() 存取（set_store 供测试注入 tmp 目录存储）。

到点面：pim 事项变更 → PimTriggerSync 注册/迁移 triggers_ext 触发器；触发
完成（trigger.fired）→ repeat 再武装。本插件不再自持轮询、不再直接出卡。
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin
from agentos_plugin_sdk.capability import bind_capability_caller
from agentos_plugin_sdk.diagnostics_settings import user_root

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根（http_json）入 sys.path

import pim_rest  # noqa: E402
from _libs.mode_server_base import html_file_response  # noqa: E402
from _root_api import (  # noqa: E402
    decode_body as _decode_body,  # noqa: E402
    error as _error,
    json_response as _json_response,
    ok as _ok,
)
from pim_focus_timer import PimFocusTimer  # noqa: E402
from pim_store import PimStore  # noqa: E402
from pim_tool import handle_pim  # noqa: E402
from pim_trigger_sync import PimTriggerSync  # noqa: E402

logger = logging.getLogger(__name__)
plugin = AgentOSPlugin("mode_personal")

_PACKAGE_DATA_DIR = Path(__file__).resolve().parent / "data"
_store: PimStore | None = None
_trigger_sync: PimTriggerSync | None = None
_focus_timer: PimFocusTimer | None = None

# 「今日」webview 页（contributes.pages pim_today 的 htmlPath 落点；形态照抄
# mode_roleplay 面板页先例：包内 HTML 文件 → text/html HttpHandleResponse）
_PAGE_ENDPOINT = "/ext/mode_personal/page/pim-today"
_PAGE_HTML_PATH = str(Path(__file__).resolve().parent / "webview" / "pim_today.html")


def user_data_dir() -> Path:
    """pim 实体域数据根（副本无关）：<user_root>/data/mode_personal/。

    数据寿命不得与插件副本绑定——同 id 副本更替（平铺↔种子区↔内置、版本
    裁决切换）时包内 data/ 不随迁（2026-10-19 面板空数据根因）。user_root
    解析与内核 core::user_space 同构（SDK diagnostics_settings 单源）。
    """
    return user_root() / "data" / "mode_personal"


def migrate_package_data(package_dir: Path, target_dir: Path) -> list[str]:
    """包内 data/ 一次性迁入用户数据根（幂等、非破坏）。

    只拷目标缺名的实体文件（``*.jsonl``/``*.json``；``.gitkeep`` 骨架标记与
    其余非实体文件不迁）——目标已有同名文件即用户侧在用（或已迁过），绝不
    覆盖。IO 失败降级为告警继续：迁移尽力而为，数据面不受阻，源文件原地
    保留下次启动可重迁。返回本次迁入的文件名列表。
    """
    if not package_dir.is_dir():
        return []
    migrated: list[str] = []
    for src in sorted(package_dir.iterdir()):
        name = src.name
        if not src.is_file() or name == ".gitkeep":
            continue
        if src.suffix not in (".jsonl", ".json"):
            continue
        target_dir.mkdir(parents=True, exist_ok=True)
        dst = target_dir / name
        if dst.exists():
            continue
        try:
            shutil.copyfile(src, dst)
        except OSError as exc:
            logger.warning("[mode_personal] data 迁移跳过 %s: %s", name, exc)
            continue
        migrated.append(name)
    return migrated


def get_store() -> PimStore:
    """数据域单例（懒构建；用户空间数据根为持久真值，首建前迁包内存量）。"""
    global _store
    if _store is None:
        target = user_data_dir()
        migrated = migrate_package_data(_PACKAGE_DATA_DIR, target)
        if migrated:
            logger.info("[mode_personal] 包内 data 已迁入用户空间 %s: %s", target, migrated)
        _store = PimStore(target)
    return _store


def set_store(store: PimStore | None) -> None:
    """替换/清空数据域单例（测试注入口；None = 复位懒构建）。"""
    global _store
    _store = store


# ══ pim 工具（LLM 面；agent_config_load 并入制入非任务管道工具面）══


@plugin.tool(
    name="pim",
    schema={
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["capture", "query", "op"],
                "description": "capture=建卡（零确认直建，回执含 op_id 可撤销）；query=读聚合；op=状态机执行",
            },
            "payload": {"type": "object", "description": "capture: {title, domain?, due?, priority?, repeat?, direction?, status?, kind?: 'event', at?} 或 {text: '- [ ] 明天 #工作 !P1 交周报'}（行语法，与页面快速添加同一解析）；query: {scope: today|todo|event|habit|partner|journal|preference, id?, status?, domain?, month?, key?, from?, to?}；op: {op: accept|complete|defer|split|wait|fail|cancel|focus_start|focus_stop|undo, entity?: todo|event, id?, todo_id?(别名), op_id?(undo), new_time?/new_due?(defer), parts?(split), duration_minutes?(focus_start，缺省取配置)}。capture 可带 pipeline_id（当前会话主管道 id）= 日程到点提醒的承载管道锚（ADR 2026-11-03-pim-trigger-anchor：提醒挂创建会话，不随活跃会话漂移）"},
        },
        "required": ["action"],
    },
    description="个人事项助理（capture/query/op，全部走 op_log 可撤销）",
)
async def pim(action: str = "", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """pim 工具 handler（三 action 分发，错误以结构化回执返回不抛出）。"""
    return handle_pim(action, payload or {}, get_store())


# ══ REST http.handle 分发（/ext/mode_personal/** 入口）══


@plugin.tool(
    name="http.handle",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "method": {"type": "string"},
            "plugin_id": {"type": "string"},
            "raw_body": {"type": "string"},
            "headers": {"type": "object"},
            "query": {"type": "object"},
        },
    },
    description="HTTP endpoint handler for /ext/mode_personal/** (个人助理 PIM REST 面)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发到 pim REST（pim_rest.route 路由核，status/payload 两元组）。"""
    del plugin_id, headers  # pim 面无 per-caller 差异（auth:user 由 dispatcher 把关）
    if path == _PAGE_ENDPOINT and method == "GET":
        try:
            return _ok(html_file_response(_PAGE_HTML_PATH))
        except OSError as exc:
            logger.exception("mode_personal page html read failed: %s", exc)
            return _error(f"pim_today html unavailable: {exc}", 500)
    try:
        body = _decode_body(raw_body)
        status, payload = pim_rest.route(get_store(), path, method, query, body)
        return _ok(_json_response(payload, status))
    except Exception as exc:  # noqa: BLE001 — 协议兜底：错误经 error 信封可见，不静默
        logger.exception("mode_personal http.handle failed: %s", exc)
        return _error(f"mode_personal service error: {exc}", 500)


# ══ 服务面（capabilities.services：pim 数据域读面，跨插件同源取数）══


@plugin.tool(
    name="pim.query",
    schema={
        "type": "object",
        "properties": {
            "payload": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "enum": ["today", "todo", "event", "habit", "partner", "journal", "preference"]},
                    "id": {"type": "string"},
                    "status": {"type": "string"},
                    "domain": {"type": "string"},
                    "month": {"type": "string"},
                    "key": {"type": "string"},
                    "from": {"type": "string"},
                    "to": {"type": "string"},
                },
                "required": ["scope"],
            }
        },
        "required": ["payload"],
    },
    description="pim 数据域读面（today 聚合/todo/habit/partner/journal；晨报与调度器同源取数）",
)
async def pim_query(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """数据域读面（只读 action=query；写面一律走工具/REST 记账通道）。"""
    return handle_pim("query", payload or {}, get_store())


def run() -> None:
    """启动 MCP 服务端。"""
    plugin.run()


# ══ 生命周期（到点触发器同步：on_load 接线 / on_unload 停止）══


def _session_creator() -> Any:
    """session.create 能力调用器（兜底承载管道，ADR 2026-11-03-pim-trigger-anchor）。

    懒解析句柄（on_load 时机早于能力声明完成）；旧内核无 session 能力时
    get_capability 抛错 → 返回恒 None 的 creator（同步层 pending 降级）。
    """

    async def _create(title: str) -> dict[str, Any] | None:
        handle = plugin.get_capability("session")
        caller = bind_capability_caller(handle, "session")  # 调用器本体不可 await（await 其返回值，同 _trigger_control_caller）
        return await caller("create", {"title": title})

    async def _create_or_none(title: str) -> dict[str, Any] | None:
        try:
            return await _create(title)
        except KeyError:
            logger.warning("[mode_personal] session 能力缺席（旧内核），兜底承载不可用")
            return None

    return _create_or_none


def _trigger_control_caller() -> Any:
    """trigger-control capability 调用器（懒解析句柄；缺失由同步层记日志降级）。"""

    async def _call(method: str, params: dict[str, Any]) -> Any:
        handle = plugin.get_capability("trigger-control")
        return await bind_capability_caller(handle, "trigger-control")(method, params)

    return _call


def _pipeline_state_caller() -> Any:
    """pipeline-state 标准能力调用器（主 agent 管道核验的 state 查询通道）。

    与 trigger-control 是两个独立能力面：state 查询必须走本通道（内核
    STANDARD_CAPABILITIES methods=[list, update]），不能搭 trigger-control
    白名单通道。能力缺席（旧内核）时 get_capability 抛 KeyError → 由同步层
    核验放行降级。
    """

    async def _call(method: str, params: dict[str, Any]) -> Any:
        handle = plugin.get_capability("pipeline-state")
        return await bind_capability_caller(handle, "pipeline-state")(method, params)

    return _call


def _focus_default_minutes(config: dict[str, Any] | None = None) -> int:
    """番茄默认时长：manifest config_files.fields 注入值，出厂缺省 25。"""
    cfg = plugin.get_config() if config is None else config
    value: Any = None
    if isinstance(cfg, dict):
        section = cfg.get("mode_personal_settings")
        if isinstance(section, dict):
            value = section.get("focus_duration_minutes")
        if value is None:
            value = cfg.get("focus_duration_minutes")
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return 25
    return minutes if 1 <= minutes <= 1440 else 25


@plugin.on_config_changed
def _on_config_changed(config: dict[str, Any]) -> None:
    """配置变更：更新番茄默认时长（进行中的番茄按启动时记录值走）。"""
    if _store is not None:
        _store.set_focus_default_minutes(_focus_default_minutes(config))


@plugin.on_load
async def _on_load(_params: dict[str, Any]) -> None:
    """sidecar 启动：事项变更观察者 + 到点触发器同步层 + 番茄定向计时上线。"""
    global _trigger_sync, _focus_timer
    store = get_store()
    store.set_focus_default_minutes(_focus_default_minutes())
    sync = PimTriggerSync(
        store,
        _trigger_control_caller(),
        session_creator=_session_creator(),
        state_caller=_pipeline_state_caller(),
    )
    focus = PimFocusTimer(store)

    def _on_change(
        entity: str,
        key: str,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        sync.on_change(entity, key, before, after)
        focus.on_change(entity, key, before, after)

    store.set_on_change(_on_change)
    focus.start()
    _trigger_sync = sync
    _focus_timer = focus
    logger.info("[mode_personal] 到点触发器同步层 + 番茄计时器已启动")


@plugin.on_unload
async def _on_unload(_params: dict[str, Any]) -> None:
    """sidecar 卸载：停止同步/计时并摘除 store 观察者（干净停止）。"""
    global _trigger_sync, _focus_timer
    store = get_store()
    store.set_on_change(None)
    if _trigger_sync is not None:
        await _trigger_sync.stop()
        _trigger_sync = None
    if _focus_timer is not None:
        await _focus_timer.stop()
        _focus_timer = None
    logger.info("[mode_personal] 到点触发器同步层 + 番茄计时器已停止")


@plugin.on_domain_event
async def _on_domain_event(params: dict[str, Any]) -> None:
    """域事件入口：会话目标跟随 / 触发完成再武装。"""
    sync = _trigger_sync
    if sync is not None:
        await sync.on_domain_event(params)


if __name__ == "__main__":
    run()
