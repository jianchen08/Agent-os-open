#!/usr/bin/env python3
"""project_manager 服务端——项目域主（资源注册收敛 T6a）。

工具面：project 合一工具（action=create|query|list|transition，工具面退役
任务C刀4；原 project_create / project_state 收敛为 action 分支）。

服务面：provides.capabilities[].namespace = ``project``（契约键 = namespace，
human / skill_manager 先例）。project_registry 登记簿自合并起由本插件私有
承载（共享根胖件吸收），消费方经 ``requires_services: ["project.<method>"]``
+ capability handle 直连（approval → human-interaction / search → skill 先例）；
wire 契约 = 工具函数裸 dict 返回，失败经 ``error`` 键表达（无信封）。
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Any

from agentos_plugin_sdk import AgentOSPlugin
from agentos_plugin_sdk.bootstrap import bootstrap_plugin
from agentos_plugin_sdk.capability import current_reverse_call_tenant_id

_paths = bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path

from project_manager import (  # noqa: E402 —— 包路径导入（共享根即组根，拍平布局）
    project_create_tool,
    project_registry,
    project_state_tool,
)

logger = logging.getLogger(__name__)

plugin = AgentOSPlugin("project_manager")


def _resolve_tenant_id(explicit: str = "") -> str:
    """授权名单租户取序（隔离执法点，1018 审计 G1）：显式形参 > 执行上下文
    身份（内核 `_identity` 注入的 contextvar，原头=调用方租户）> default。

    与 tasks/storage.py 取序同源。禁止回落写死 default 作首取——名单文件是
    per-租户用户空间真值（config/users/{tenant}/project_whitelist.yaml），
    错租户即授权串租（grant 落 admin 名下 / 别户名单判我写的闸）。
    """
    tenant = str(explicit or "").strip()
    if tenant:
        return tenant
    return current_reverse_call_tenant_id() or project_registry.DEFAULT_TENANT

# schema/description 单一事实源：各自 tool 模块的 get_tool_definition()
_PROJECT_CREATE_DEF = project_create_tool.ProjectCreateTool.get_tool_definition()
_PROJECT_STATE_DEF = project_state_tool.ProjectStateTool.get_tool_definition()

# ── project：项目操作面（create / query / list / transition 合一） ────────
# 原 project_create / project_state 两件按工具面审查 §十 合并计划收敛为一件
# （工具面退役·任务C刀4）：action 必填枚举，oneOf 按 action 硬隔离参数集
# （create 与 query/list/transition 参数完全不相交）；create 侧 project_type
# 仍由 config/tools/project_create.yaml 生成（映射唯一真值不变）；实现体
# ProjectCreateTool / ProjectStateTool 原地复用不复制。
_PROJECT_ACTION_DESC = (
    "项目动作（必填枚举）：create=创建项目（真实文件夹+登记行+类型出生初始化）；"
    "query=按 project_id 读登记行方案工作流状态；list=列举全部登记项目"
    "（按更新时间倒序）；transition=申请状态迁移（触发用户审批）"
)


def _build_project_input_schema() -> dict[str, Any]:
    """合并 input_schema：action 选择器 + create/state 两侧参数 + oneOf 硬隔离。

    create 侧 project_type 由 config/tools/project_create.yaml 生成（原锁步
    契约保持：配置改动不同步 manifest = 锁步测试红 + G2 拒注册）。
    """
    create_props = dict(_PROJECT_CREATE_DEF.input_schema["properties"])
    state_props = dict(_PROJECT_STATE_DEF.input_schema["properties"])
    properties: dict[str, Any] = {"action": {
        "type": "string",
        "enum": ["create", "query", "list", "transition"],
        "description": _PROJECT_ACTION_DESC,
    }}
    properties.update(create_props)
    properties.update(state_props)
    return {
        "type": "object",
        "properties": properties,
        "required": ["action"],
        "oneOf": [
            {
                "properties": {
                    "action": {"const": "create"},
                    "goal": {},
                    "path": {},
                    "mode_tags": {},
                    "default_mode": {},
                    "project_type": {},
                },
                "required": ["action", "goal"],
                "additionalProperties": False,
            },
            {
                "properties": {
                    "action": {"const": "query"},
                    "project_id": {},
                },
                "required": ["action", "project_id"],
                "additionalProperties": False,
            },
            {
                "properties": {
                    "action": {"const": "list"},
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            {
                "properties": {
                    "action": {"const": "transition"},
                    "project_id": {},
                    "target_state": {},
                    "reason": {},
                },
                "required": ["action", "project_id", "target_state", "reason"],
                "additionalProperties": False,
            },
        ],
    }


_PROJECT_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "description": (
        "按 action 返回对应结构：create={project_id,title,path,status,created,"
        "workflow_state?,session_id?,mode_tags?,default_mode?,warnings?,"
        "project_type?,init?}；query/transition=单项目快照（project_id/"
        "workflow_state/title/path/mode_tags/default_mode/transitioned）；"
        "list={projects 数组 + total}；参数错误返回 {success:false,error}"
        "（error 教改法）"
    ),
    "properties": {
        "project_id": {"type": "string", "description": "项目 id（12hex；复用既有时同值）"},
        "title": {"type": "string", "description": "项目标题"},
        "path": {"type": "string", "description": "项目文件夹宿主绝对路径"},
        "status": {"type": "string", "description": "active | paused"},
        "workflow_state": {
            "type": "string",
            "description": "方案工作流状态 plan | running | done（ADR 2026-09-17）",
        },
        "created": {"type": "boolean", "description": "true=新建登记；false=复用既有"},
        "session_id": {"type": "string", "description": "创建时关联的会话（可选）"},
        "mode_tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "项目登记模式标签集合（未登记为空数组，批 B8）",
        },
        "default_mode": {
            "type": "string",
            "description": "主域标注（mode_tags 子集，未标注为空串）",
        },
        "warnings": {
            "type": "array",
            "items": {"type": "string"},
            "description": "技能播种告警（登记成功照常返回；包不可解析/单技能复制"
                           "失败在此可见，不静默）",
        },
        "project_type": {"type": "string", "description": "路由到的项目类型（未路由为空串）"},
        "init": {
            "type": ["object", "null"],
            "description": "出生初始化执行明细（未路由为 null）",
            "properties": {
                "addons_installed": {"type": "array", "items": {"type": "string"},
                                     "description": "本次新装的宿主插件名"},
                "addons_present": {"type": "array", "items": {"type": "string"},
                                   "description": "已就位未重复安装的插件名"},
                "project_file_created": {"type": "boolean",
                                         "description": "类型清单文件是否本次脚手架创建"},
                "enabled_added": {"type": "array", "items": {"type": "string"},
                                   "description": "本次新增的启用条目"},
                "ci": {
                    "type": "object",
                    "description": (
                        "CI 包出生组装报告（设计 docs/working/项目类型CI包设计_20261030.md"
                        " §6.2：启用/跳过的面 + 判定原因）"
                    ),
                    "properties": {
                        "installed": {"type": "boolean",
                                      "description": "是否执行了 CI 包装配"},
                        "packs": {"type": "object",
                                  "description": "装配的 pack → 出厂版本"},
                        "packs_installed": {"type": "array", "items": {"type": "string"},
                                            "description": "本次实际拷写工厂物料的 pack"},
                        "packs_skipped": {"type": "array", "items": {"type": "string"},
                                          "description": "跳过的 pack 与原因（未出厂/版本闸）"},
                        "gates_total": {"type": "integer",
                                        "description": "组装后门禁总数"},
                        "hook_installed": {"type": "boolean",
                                           "description": "pre-commit 钩子是否本次写入"},
                        "hook_skipped_reason": {"type": "string",
                                                "description": "钩子未写入的原因（空串=已写入）"},
                        "probes": {"type": "object",
                                   "description": "出生工具探测结果（godot_bin 路径或空串）"},
                        "warnings": {"type": "array", "items": {"type": "string"},
                                     "description": "装配告警（钩子冲突/local 未触碰等，不静默）"},
                    },
                },
                "committed": {"type": "boolean",
                              "description": "初始化后是否执行了 git 提交（无变更则跳过为 false）"},
            },
        },
        "transitioned": {"type": "boolean", "description": "本次是否发生迁移（query 恒 false）"},
        "projects": {
            "type": "array",
            "description": "list 结果：全部登记项目，按 updated_at 倒序",
            "items": {
                "type": "object",
                "required": ["project_id", "workflow_state"],
                "properties": {
                    "project_id": {"type": "string"},
                    "title": {"type": "string"},
                    "workflow_state": {"type": "string", "description": "plan | running | done"},
                    "status": {"type": "string", "description": "active | paused（文件夹生命周期）"},
                    "auto_execute": {"type": "boolean"},
                    "path": {"type": "string"},
                    "mode_tags": {"type": "array", "items": {"type": "string"}},
                    "default_mode": {"type": "string"},
                    "updated_at": {"type": "string"},
                },
            },
        },
        "total": {"type": "integer", "description": "list 结果：项目总数"},
    },
}

_PROJECT_DESCRIPTION = (
    "项目操作面（四动作合一）。"
    "action='create'：创建项目（= 真实文件夹 + 登记行 + 类型出生初始化）。项目是"
    "任务树分组锚点，不执行、无执行者；创建后把 project_id 传给 task_submit 挂靠，"
    "任务即在项目文件夹下执行（默认 worktree 分叉）。同目录已登记时复用既有项目"
    "（created=false）并幂等补装类型初始化。最小示例："
    "project(action='create', goal='<项目标题>')（path/mode_tags/default_mode/"
    "project_type 可选，project_type 映射说明见参数说明）。"
    "action='query'：按 project_id 读登记行方案工作流状态。最小示例："
    "project(action='query', project_id='<12hex>')。"
    "action='list'：列举全部登记项目（无需 project_id，按更新时间倒序，含 "
    "title/path/status）。最小示例：project(action='list')。"
    "action='transition'：申请状态迁移（plan→running 定稿过门 / running→plan "
    "修订回门），迁移触发用户审批（安全规则 needs_approval），审批通过前状态不变；"
    "plan 态内 task_submit 仅允许派发调研/环境准备类任务，执行类任务会被状态闸拒绝；"
    "done 由用户在界面管理，不在工具可迁集合内。最小示例："
    "project(action='transition', project_id='<12hex>', target_state='running', "
    "reason='<定稿说明>')。"
)

_CREATE_ONLY_PARAMS = ("goal", "path", "mode_tags", "default_mode", "project_type")
_STATE_ONLY_PARAMS = ("project_id", "target_state", "reason")


async def _run_project_tool(tool: Any, kwargs: dict[str, Any]) -> dict[str, Any]:
    result = await tool.execute(kwargs)
    if result.success and isinstance(result.output, dict):
        return result.output
    return {"success": False, "error": result.error}


@plugin.tool(
    name="project",
    schema=_build_project_input_schema(),
    description=_PROJECT_DESCRIPTION,
)
async def project(action: str = "", **kwargs: dict[str, Any]) -> dict[str, Any]:
    """按 action 分发到创建/状态实现体；跨动作参数运行时教学错误（错误教改法）。"""
    if action == "create":
        foreign = [k for k in _STATE_ONLY_PARAMS if k in kwargs]
        if foreign:
            return {"success": False, "error": (
                f"{', '.join(foreign)} 仅 action='query'/'transition' 可用；create 必填 "
                "goal（其余可选），示例: project(action='create', goal='<项目标题>')")}
        return await _run_project_tool(project_create_tool.ProjectCreateTool(), kwargs)
    if action in ("query", "list", "transition"):
        foreign = [k for k in _CREATE_ONLY_PARAMS if k in kwargs]
        if foreign:
            return {"success": False, "error": (
                f"{', '.join(foreign)} 仅 action='create' 可用；query/transition 必填 "
                "project_id（transition 另需 target_state/reason），list 无需 id，"
                "示例: project(action='list')")}
        return await _run_project_tool(
            project_state_tool.ProjectStateTool(), {**kwargs, "action": action})
    return {"success": False, "error": (
        f"action 非法: {action!r}（允许: 'create' | 'query' | 'list' | 'transition'）；"
        "示例: project(action='create', goal='<项目标题>') 或 project(action='list')")}


# ─── project.* 服务面（消费方 capability 直连；阶段 2 起接线消费）──────────


def _require_row(project: Any) -> dict[Any, Any] | None:
    """服务参数行防御：非 dict / 空行 = None（调用方回 error）。"""
    if not isinstance(project, dict) or not project:
        return None
    return project


@plugin.tool(
    name="project.get",
    schema={
        "type": "object",
        "properties": {
            "project_id": {"type": "string", "description": "项目 id（12hex 登记键）"}
        },
        "required": ["project_id"],
    },
    description="Get one project registration row; unknown id → found=false",
)
async def project_get(project_id: str = "") -> dict[str, Any]:
    """单项目登记行快照；未知 id 不算失败（found=false）。"""
    project = project_registry.ProjectRegistry().get(str(project_id or "").strip())
    if project is None:
        return {"found": False}
    return {"found": True, "project": asdict(project)}


@plugin.tool(
    name="project.list",
    schema={"type": "object", "properties": {}},
    description="List all project registration rows (created_at desc)",
)
async def project_list() -> dict[str, Any]:
    """全量登记行（创建时间倒序；消费侧过滤/分页）。"""
    rows = [asdict(p) for p in project_registry.ProjectRegistry().list()]
    return {"items": rows, "total": len(rows)}


@plugin.tool(
    name="project.save",
    schema={
        "type": "object",
        "properties": {
            "project": {
                "type": "object",
                "description": "完整登记行（读改写语义：取行改字段后整行回存）",
            }
        },
        "required": ["project"],
    },
    description="Save one full project row (mode-field change validation on write face)",
)
async def project_save(project: dict[str, Any] | None = None) -> dict[str, Any]:
    """整行回存（mode 字段变更时写面校验）；行非法/校验失败 → error 键。"""
    row = _require_row(project)
    if row is None:
        return {"error": "project row required"}
    try:
        saved = project_registry.ProjectRegistry().save(project_registry.ProjectModel(**row))
    except TypeError as exc:
        return {"error": f"project row invalid: {exc}"}
    except ValueError as exc:
        return {"error": str(exc)}
    return {"project": asdict(saved)}


@plugin.tool(
    name="project.delete",
    schema={
        "type": "object",
        "properties": {"project_id": {"type": "string"}},
        "required": ["project_id"],
    },
    description="Delete one registration row (folder untouched; caller decides)",
)
async def project_delete(project_id: str = "") -> dict[str, Any]:
    """删登记行（不动文件夹）；行不存在 = deleted=false。"""
    deleted = project_registry.ProjectRegistry().delete(str(project_id or "").strip())
    return {"deleted": deleted}


@plugin.tool(
    name="project.create",
    schema={
        "type": "object",
        "properties": {
            "title": {"type": "string", "minLength": 1},
            "path": {"type": "string"},
            "session_id": {"type": "string"},
            "submitted_by": {"type": "string"},
            "auto_execute": {"type": "boolean"},
            "mode_tags": {"type": "array", "items": {"type": "string"}},
            "default_mode": {"type": "string"},
        },
        "required": ["title"],
    },
    description="Create/register a project folder (idempotent by path; reuse → created=false)",
)
async def project_create_service(
    title: str = "",
    *,
    path: str = "",
    session_id: str = "",
    submitted_by: str = "",
    auto_execute: bool = False,
    mode_tags: list[str] | None = None,
    default_mode: str | None = None,
) -> dict[str, Any]:
    """按路径幂等登记（ensure_project_registered 语义：同路径复用既有行）。"""
    if not str(title or "").strip():
        return {"error": "title required"}
    try:
        project, created = project_registry.ensure_project_registered(
            title=str(title),
            explicit_path=str(path or ""),
            session_id=str(session_id or ""),
            submitted_by=str(submitted_by or ""),
            auto_execute=bool(auto_execute),
            mode_tags=mode_tags,
            default_mode=default_mode,
        )
    except (ValueError, RuntimeError) as exc:
        return {"error": str(exc)}
    return {"project": asdict(project), "created": created}


@plugin.tool(
    name="project.list_paths",
    schema={"type": "object", "properties": {}},
    description="Lightweight read: project_id → folder path map",
)
async def project_list_paths() -> dict[str, Any]:
    """轻量只读解析（每次直读登记目录；消费方按 id 取用）。"""
    return {"paths": project_registry.load_project_paths()}


@plugin.tool(
    name="project.remove_folder",
    schema={
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    },
    description="Delete a project folder (destructive; guarded roots refused → removed=false)",
)
async def project_remove_folder(path: str = "") -> dict[str, Any]:
    """删项目文件夹（路径安全校验内置：盘符根/仓库根/工作空间基拒删）。"""
    removed = project_registry.remove_project_folder(str(path or ""))
    return {"removed": removed}


@plugin.tool(
    name="project.load_zones",
    schema={
        "type": "object",
        "properties": {
            "tenant_id": {"type": "string", "description": "显式租户（缺省取执行上下文身份)"},
        },
    },
    description="Load all three zone-policy sections (entries/read_deny/read_allow) in one read",
)
async def project_load_zones(tenant_id: str = "") -> dict[str, Any]:
    """授权名单三节一次读全（同一份 YAML 单次解析；判定链各取所需）。"""
    return project_registry.load_zone_sections(tenant_id=_resolve_tenant_id(tenant_id))


@plugin.tool(
    name="project.add_zone",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "section": {
                "type": "string",
                "enum": ["entries", "read_deny", "read_allow"],
                "description": "entries=写区 read_deny=读排除 read_allow=读授权",
            },
            "tenant_id": {"type": "string", "description": "显式租户（缺省取执行上下文身份)"},
        },
        "required": ["path"],
    },
    description="Append one zone-policy entry (section-scoped; whole-drive roots refused)",
)
async def project_add_zone(
    path: str = "", section: str = "entries", tenant_id: str = ""
) -> dict[str, Any]:
    """追加名单条目（授权卡永久落盘 / 设置页共用通道）；非法节或路径 → error 键。"""
    try:
        entries = project_registry.add_zone_entry(
            str(path or ""), section=str(section or "entries"), tenant_id=_resolve_tenant_id(tenant_id)
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"entries": entries}


@plugin.tool(
    name="project.remove_zone",
    schema={
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "section": {"type": "string", "enum": ["entries", "read_deny", "read_allow"]},
            "tenant_id": {"type": "string", "description": "显式租户（缺省取执行上下文身份)"},
        },
        "required": ["path"],
    },
    description="Remove one zone-policy entry (idempotent)",
)
async def project_remove_zone(
    path: str = "", section: str = "entries", tenant_id: str = ""
) -> dict[str, Any]:
    """移除名单条目（幂等）；非法节 → error 键。"""
    try:
        entries = project_registry.remove_zone(
            str(path or ""), section=str(section or "entries"), tenant_id=_resolve_tenant_id(tenant_id)
        )
    except ValueError as exc:
        return {"error": str(exc)}
    return {"entries": entries}


@plugin.tool(
    name="project.purge_legacy_dirs",
    schema={"type": "object", "properties": {}},
    description="One-time legacy cleanup: remove container_* dirs under workspace base (idempotent)",
)
async def project_purge_legacy_dirs() -> dict[str, Any]:
    """容器任务遗留目录清除（幂等；tasks 启动序列消费）。"""
    return {"removed_dirs": project_registry.purge_legacy_workspace_dirs()}


if __name__ == "__main__":
    plugin.run()
