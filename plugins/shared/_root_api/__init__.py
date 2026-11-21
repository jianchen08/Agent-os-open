"""共享根显式 API 出口（审查总册 E2，2026-10-16 拍板「API 化+死代码清理」）。

plugins/shared 根直下 28 件裸模块的公共 API 于此显式声明：生产消费方一律
``from _root_api import ...``（含模块绑定名），禁止裸名 import 实现模块
（scripts/check_shared_root_imports.py 闸执法；tests 与共享根实现区豁免）。

形态 = 名单声明 + PEP 562 ``__getattr__`` call-time 转发（经 sys.modules
解析承载模块），与裸名 import 全 seam 语义等价——测试对源模块的
monkeypatch / sys.modules 替换 / 惰性 import 时序在两种形态下同观感。
出口名单 = 生产消费方实际 from-import 名字全集 + attr 式消费的模块绑定；
新增名字/根模块须同步 ``_EXPORTS``/``_MODULE_EXPORTS``（禁区由闸动态
枚举，新落根模块未入名单即对消费方违规）。逐件判定与消费清单见
docs/working/E2共享根分诊_台账.md（R1 普查）。
"""

from __future__ import annotations

import importlib
import sys
from typing import Any

# 名字 → 承载模块（属性名单，API 声明单源，按承载模块分组）
_EXPORTS: dict[str, str] = {
    # —— tenant_data：租户数据根解析
    "DEFAULT_TENANT": "tenant_data",
    # —— db_read：内核库只读查询面
    "DbReadQueryError": "db_read",
    "db_read_query": "db_read",
    # —— hlc_clock：混合逻辑时钟
    "HlcTimestamp": "hlc_clock",
    "HybridLogicalClock": "hlc_clock",
    # —— mode_keys：模式键两级解析（T6a：project_registry 吸收件经出口消费）
    "MODE_ID_RE": "mode_keys",
    # —— outbox_store：outbox 投递存储
    "OutboxStore": "outbox_store",
    # —— mode_keys：模式键两级解析（T6a：常量收编 mode_keys，与 planning_executors 聚合面同居）
    "PLAN_DISPATCH_WHITELIST": "mode_keys",
    # —— sync_event：同步事件契约与存储
    "ASSET_CHANNELS": "sync_event",
    "CHANNEL_OWNER_SCOPED": "sync_event",
    "SyncEvent": "sync_event",
    "SyncStore": "sync_event",
    # —— task_birth：任务诞生管道
    "TaskBirthError": "task_birth",
    # —— rp_state_audit：RP 状态审计事件（裁定件）
    "append_audit_rows": "rp_state_audit",
    # —— state_fields：state 字段两形态容忍单点
    "as_dict": "state_fields",
    # —— atomic_io：原子写文件（temp+replace）
    "atomic_write_text": "atomic_io",
    # —— task_birth：任务诞生管道
    "birth_task_pipeline": "task_birth",
    # —— task_birth：工作空间准备失败的阶段标签（失败错误码选择判据）
    "WORKSPACE_PREPARE_PHASE": "task_birth",
    # —— wiring：capability 调用面装配
    "build_memory_backend": "wiring",
    # —— mode_keys：模式键两级解析
    "collect_mode_inject_tool_ids": "mode_keys",
    "collect_mode_tool_ids": "mode_keys",
    "collect_planning_executors": "mode_keys",
    # —— http_json：HTTP 响应封装样板
    "decode_body": "http_json",
    # —— sync_event：通道注册表（路由中间件，ADR 2026-10-17）
    "asset_channel": "sync_event",
    # —— kernel_token：内核 token 解码
    "decode_kernel_token": "kernel_token",
    # —— isolation_path：隔离工作空间路径解析
    "ensure_isolation_path": "isolation_path",
    # —— http_json：HTTP 响应封装样板
    "error": "http_json",
    # —— mode_keys：模式键两级解析
    "find_mode_agent_yaml": "mode_keys",
    "find_mode_package_dir": "mode_keys",
    "primary_mode_tag": "mode_keys",
    "resolve_active_mode": "mode_keys",
    # —— rp_state_audit：RP 状态审计事件（裁定件）
    "has_idem": "rp_state_audit",
    # —— mode_keys：模式键两级解析
    "is_planning_executor_key": "mode_keys",
    "is_skill_name": "mode_keys",
    # —— ulid_keys：ULID 生成与校验
    "is_valid_ulid": "ulid_keys",
    # —— _llm_streaming：流式协议翻译真源（llm 兼容 shim 经出口消费）
    # —— time_iso：UTC 时间戳单点
    "iso_sort_key": "time_iso",
    # —— http_json：HTTP 响应封装样板
    "json_response": "http_json",
    # —— proc_tree：进程树终止
    "kill_process_tree": "proc_tree",
    # —— rp_state_audit：RP 状态审计事件（裁定件）
    "last_assistant_seq": "rp_state_audit",
    # —— tenant_data：租户数据根解析（T6a：project_manager 吸收件经出口消费）
    "legacy_config_users_base": "tenant_data",
    # —— wiring：capability 调用面装配
    "make_capability_caller": "wiring",
    # —— ulid_keys：ULID 生成与校验
    "new_ulid": "ulid_keys",
    # —— _llm_streaming：流式协议翻译真源（llm 兼容 shim 经出口消费）
    "DegenerationDetector": "_llm_streaming",
    "StreamEvent": "_llm_streaming",
    "StreamTranslator": "_llm_streaming",
    "map_finish_reason": "_llm_streaming",
    # —— time_iso：UTC 时间戳单点
    "now_iso_utc": "time_iso",
    # —— http_json：HTTP 响应封装样板
    "ok": "http_json",
    # —— mode_keys：模式键两级解析
    "parse_mode_agent_key": "mode_keys",
    # —— http_json：HTTP 响应封装样板
    "parse_multipart": "http_json",
    "protocol_error": "http_json",
    # —— rp_state_audit：RP 状态审计事件（裁定件）
    "recent_events": "rp_state_audit",
    # —— project_root：项目根解析单源
    "resolve_project_root": "project_root",
    # —— repo_anchor：仓库锚定读写裁决
    "resolve_repo_root": "repo_anchor",
    # —— uploads_path：uploads URL 解析
    "resolve_uploads_url": "uploads_path",
    # —— rp_state_audit：RP 状态审计事件（裁定件）
    "story_idem": "rp_state_audit",
    "suffixed": "rp_state_audit",
    # —— tenant_data：租户数据根解析（T6a：project_manager 吸收件经出口消费）
    "tenant_config_dir": "tenant_data",
    # —— tenant_data：租户数据根解析
    "tenant_data_root": "tenant_data",
    # —— user_space：用户空间目录单源
    "user_config_dir": "user_space",
    "user_data_dir": "user_space",
    "user_plugins_dir": "user_space",
    "user_root": "user_space",
    # —— sync_event：属主面判定（relay 执法与端侧镜像唯一判定源）
    "is_owner_scoped": "sync_event",
    # —— sync_event：同步事件契约与存储
    "validate_event": "sync_event",
}

# 模块绑定名单（attr 式消费：消费方拿模块对象按属性访问，名字自映射）
_MODULE_EXPORTS: dict[str, str] = {
    "multimodal_patterns": "multimodal_patterns",  # 多模态正则词汇单源
    "repo_anchor": "repo_anchor",  # 仓库锚定读写裁决
    "state_fields": "state_fields",  # state 字段两形态容忍单点
    "user_space": "user_space",  # 用户空间目录单源
    "zone_policy": "zone_policy",  # 授权区 state 键单源
}

__all__ = [
    "ASSET_CHANNELS",
    "CHANNEL_OWNER_SCOPED",
    "DEFAULT_TENANT",
    "DbReadQueryError",
    "HlcTimestamp",
    "HybridLogicalClock",
    "MODE_ID_RE",
    "OutboxStore",
    "PLAN_DISPATCH_WHITELIST",
    "SyncEvent",
    "SyncStore",
    "TaskBirthError",
    "WORKSPACE_PREPARE_PHASE",
    "append_audit_rows",
    "as_dict",
    "atomic_write_text",
    "birth_task_pipeline",
    "build_memory_backend",
    "collect_mode_inject_tool_ids",
    "collect_mode_tool_ids",
    "collect_planning_executors",
    "asset_channel",
    "db_read_query",
    "decode_body",
    "decode_kernel_token",
    "ensure_isolation_path",
    "error",
    "find_mode_agent_yaml",
    "find_mode_package_dir",
    "has_idem",
    "is_owner_scoped",
    "is_planning_executor_key",
    "is_skill_name",
    "is_valid_ulid",
    "iso_sort_key",
    "json_response",
    "kill_process_tree",
    "last_assistant_seq",
    "legacy_config_users_base",
    "make_capability_caller",
    "multimodal_patterns",
    "new_ulid",
    "now_iso_utc",
    "ok",
    "parse_mode_agent_key",
    "parse_multipart",
    "primary_mode_tag",
    "protocol_error",
    "recent_events",
    "repo_anchor",
    "resolve_active_mode",
    "resolve_project_root",
    "resolve_repo_root",
    "resolve_uploads_url",
    "state_fields",
    "DegenerationDetector",
    "StreamEvent",
    "StreamTranslator",
    "map_finish_reason",
    "story_idem",
    "suffixed",
    "tenant_config_dir",
    "tenant_data_root",
    "user_config_dir",
    "user_data_dir",
    "user_plugins_dir",
    "user_root",
    "user_space",
    "validate_event",
    "zone_policy",
]


def __getattr__(name: str) -> Any:
    """PEP 562 出口转发：call-time 解析，语义与裸名 import 等价。"""
    bind = _MODULE_EXPORTS.get(name)
    if bind is not None:
        module = sys.modules.get(bind)
        if module is None:
            module = importlib.import_module(bind)
        return module
    mod_name = _EXPORTS.get(name)
    if mod_name is None:
        raise AttributeError(f"module 'plugins.shared._root_api' 出口名单外: {name!r}")
    module = sys.modules.get(mod_name)
    if module is None:
        module = importlib.import_module(mod_name)
    return getattr(module, name)


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_EXPORTS) | set(_MODULE_EXPORTS))
