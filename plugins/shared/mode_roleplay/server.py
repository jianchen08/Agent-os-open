"""mode_roleplay 插件 HTTP 路由装配（总路由分发；域实现见 rp_* 子模块）。"""
from __future__ import annotations

from typing import Any

from agentos_plugin_sdk.bootstrap import bootstrap_plugin

bootstrap_plugin(__file__)  # 插件目录 + plugins/shared 根入 sys.path——必须先于 rp_* 兄弟层导入：兄弟层模块加载期即需 _libs 共享根，仅 bootstrap 注入可达（sidecar spawn 不注入 PYTHONPATH）

# ── 职责域子模块（自本文件按域拆分；裸名同目录 import；bootstrap 后导入面）──
# noqa F401：http_handle/声明面经 server 模块属性访问（模块属性兼容面）
# bootstrap 后导入面（_libs 单源三件套同款惰性位置）
from _libs.card_contract import flatten_variables  # noqa: E402,F401
from _libs.mode_server_base import (  # noqa: E402
    html_file_response,  # noqa: F401
    html_text_response,  # noqa: F401
    load_mode_profile,  # noqa: F401
    register_profile_services,  # noqa: F401
)
from rp_cards import (  # noqa: E402,F401
    _AI_CREATOR_MARK,
    _CREATION_CATEGORY,
    _DRAFT_REREGISTER_FROM,
    _DRAFT_ROUTE_PREFIX,
    _DRAFT_STATES,
    _DRAFT_TRANSITIONS,
    _DRAFTS_ROUTE,
    _PRECIP_BOOKKEEPING_ACTIONS,
    _PRECIP_CANDIDATES_ROUTE,
    _PRECIP_PROMOTE_ROUTE,
    _PRECIP_SOURCE,
    _PRECIP_STAT_WINDOW,
    _book_from_data,
    _card_from_data,
    _card_id_from_filename,
    _card_vocab,
    _combo_delete_request,
    _combo_from_data,
    _combo_save_request,
    _combos_raw,
    _delete_card_request,
    _delete_lorebook_request,
    _derive_card_id,
    _entry_from_st,
    _export_card_request,
    _export_envelope,
    _import_card_request,
    _import_lorebook_request,
    _llm_default_chat_model,
    _llm_yaml_candidates,
    _load_cards,
    _load_combos,
    _load_lorebooks,
    _norm_messages,
    _norm_sessions,
    _persona_delete_request,
    _persona_save_request,
    _personas_load,
    _personas_path,
    _personas_raw,
    _save_card_request,
    _save_lorebook_request,
    _scan_books_dir,
    _scan_card_danger,
    _scan_card_html_danger,
    _scan_cards_dir,
    _scan_styles_dir,
    _session_pack_ids,
    _session_state_summary,
    _st_world_info_entries,
    _user_cards_dir,
    _user_combos_dir,
    _user_config_root,
    _user_lorebooks_dir,
    _wizard_axes_listing,
    _write_personas,
    _write_user_card,
    _write_user_lorebook,
    card_lint_actions,
    card_lint_decl,
    card_lint_extensions_service,
    card_lint_pack_ui,
    card_scan_html_card,
    card_scan_html_texts,
    card_scan_suspicious,
    card_texts_of_tree,
    lint_operations,
    load_profile,
    mode_describe,
    mode_get_profile,
)
from rp_common import (  # noqa: E402,F401
    TICK_WHEN_OPS,
    _AGENCY_AXIS_OPTIONS,
    _AGENTS_DIR,
    _BRIDGE_INCLUDE_MARK,
    _BRIDGE_SOURCE_RE,
    _CARD_ID_RE,
    _CORE_INCLUDE_MARK,
    _CORE_SOURCE_RE,
    _GAME_HOST_ID_RE,
    _LOREBOOKS_DIR,
    _PACE_AXIS_OPTIONS,
    _PACK_ID_RE,
    _PACKS_DIR,
    _PAGE_ASSEMBLIES,
    _PANEL_HTML_PATH,
    _PROFILE_PATH,
    _PROVIDERS,
    _SANITIZE_INCLUDE_MARK,
    _SANITIZE_SOURCE_RE,
    _SCALE_AXIS_OPTIONS,
    _STYLES_DIR,
    _WARNED,
    _call_provider,
    _game_routed,
    _json_response,
    _on_load,
    _pack_tick_listing,
    _resolve_workspace,
    _set_provider,
    lint_tick,
    logger,
    plugin,
    reset_providers,
)
from rp_content import (  # noqa: E402,F401
    _COMBOS_ROUTE_DELETE,
    _COMBOS_ROUTE_SAVE,
    _PACKS_ROUTE,
    _SCENE_LLM_TIMEOUT_SECS,
    _SCENE_SYSTEM_PROMPT,
    _SCENE_TURN_ROUTE,
    _WIZARD_AXES_ROUTE,
    _aggregate_game_decls,
    _aggregate_pack_init_ui,
    _assemble_pack_page,
    _assemble_page_single_source,
    _handle_game_config,
    _handle_game_page,
    _handle_game_page_by_pack,
    _handle_scene_turn,
    _inject_page_theme,
    _load_material,
    _load_pack_assets,
    _load_packs_listing,
    _merge_game_actions,
    _norm_pack_choices,
    _pack_from_data,
    _pack_init_ui_listing,
    _pack_operations_listing,
    _pack_page_asset,
    _pack_pipeline_listing,
    _pack_rel_path_in_dir,
    _pack_ui_listing,
    _precip_effect_value,
    _precip_event_turn,
    _precip_is_number,
    _precipitation_promote_request,
    _precipitation_stream,
    _read_yaml_dict,
    _registered_pipeline_names,
    _scan_packs_dir,
    _scene_card_state_text,
    _scene_json_entries,
    _scene_llm_text,
    _scene_material_injection,
    _scene_participants_resolve,
    _scene_prompt,
    _scene_reactions_parse,
    _scene_visibility_resolve,
    _user_packs_dir,
    _user_pipelines_dir,
    precipitation_candidates,
)
from rp_game import (  # noqa: E402,F401
    _ACTION_ROUTES,
    _ADJUDICATE_SERVICE_PLUGIN,
    _BODY_INVALID,
    _DATA_ROUTES,
    _DRAFT_HANDLERS,
    _GAME_ATTACH_ROUTE,
    _GAME_CHANNEL_CAP,
    _GAME_CHANNELS,
    _GAME_CONFIG_ROUTE,
    _GAME_DETACH_ROUTE,
    _GAME_OPERATION_ROUTE,
    _GAME_PAGE_DATA_ROUTE,
    _GAME_PAGE_ENDPOINT,
    _GAME_PAGE_HTML_PATH,
    _GAME_SNAPSHOT_ROUTE,
    _GAME_STATE_ROUTE,
    _GAME_TICK_ROUTE,
    _GAME_TURN_ROUTE,
    _PANEL_ENDPOINT,
    _SOURCE_OPERATION,
    _SOURCE_TICK,
    _TICK_MAX_TICKS,
    _TURN_SERVICE_PLACEHOLDERS,
    _TURN_SERVICES,
    _draft_approve_request,
    _draft_card_from_content,
    _draft_lint_request,
    _draft_read,
    _draft_register_request,
    _draft_reject_request,
    _draft_smoke_request,
    _draft_submit_request,
    _draft_transition_guard,
    _draft_write,
    _drafts_query,
    _game_channel_id,
    _game_page_html_response,
    _handle_draft_routes,
    _handle_game_attach,
    _handle_game_detach,
    _handle_game_operation,
    _handle_game_snapshot,
    _handle_game_state,
    _handle_game_tick,
    _handle_game_turn,
    _handle_play,
    _invoke_tool,
    _is_game_index,
    _operation_reject_reason,
    _panel_html_response,
    _parse_body,
    _reset_game_channels,
    _routed_json,
    _tick_baseline,
    _tick_cycle_next,
    _tick_step_changes,
    _tick_when_met,
    _user_drafts_dir,
    _user_id_from_headers,
    _validate_operation_params,
    mode_game_push_delta,
)


async def _handle_actions(path: str, method: str, raw_body: str) -> dict[str, Any] | None:
    """写动作分发；未命中返回 None（交回 404 路径）。"""
    if path not in _ACTION_ROUTES:
        return None
    if method != "POST":
        return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
    body = _parse_body(raw_body)
    if path.endswith("/play"):
        return await _handle_play(body)
    if path.endswith("/regenerate"):
        # 已收编：变体生成在扮演会话内走宿主消息操作（重新生成/‹i·n› 多代
        # 切换），本动作面退役——保留 410 语义化响应而非 404，旧面板缓存可辨。
        return {"success": True,
                "data": _json_response({"error": "regenerate 已收编进扮演会话的宿主消息操作"}, 410)}
    if path.endswith("/cards/save"):
        return _routed_json(_save_card_request(body))
    if path.endswith("/cards/delete"):
        return _routed_json(_delete_card_request(body))
    if path.endswith("/cards/import"):
        return _routed_json(_import_card_request(body))
    if path.endswith("/cards/export"):
        return _routed_json(_export_card_request(body))
    if path.endswith("/lorebooks/save"):
        return _routed_json(_save_lorebook_request(body))
    if path.endswith("/lorebooks/delete"):
        return _routed_json(_delete_lorebook_request(body))
    if path.endswith("/lorebooks/import"):
        return _routed_json(_import_lorebook_request(body))
    if path.endswith("/personas/save"):
        return _routed_json(_persona_save_request(body))
    if path.endswith("/combos/save"):
        return _routed_json(_combo_save_request(body))
    if path.endswith("/combos/delete"):
        return _routed_json(_combo_delete_request(body))
    if path.endswith("/group/scene_turn"):
        # 场景批一次调用编排（缺口批次四）：校验分诊显式（缺参/会话/卡库/可见性
        # 400，模型档/LLM 传输面 502），不走 _routed_json 两档映射——沉淀域段
        # 元组先例同款理由（状态语义多档，映射面表达不了）。
        return await _handle_scene_turn(body)
    if path.endswith("/precipitation/promote"):
        # 审核闸固化（缺口批次三）：显式调用才写用户层玩法包——人工审核即端点
        # 语义；状态分诊显式（校验 400/包不存在 404/作者面冲突 409），draft 域
        # 元组先例同款，不走 _routed_json 两档映射。
        payload, status = _precipitation_promote_request(body)
        return {"success": True, "data": _json_response(payload, status)}
    return _routed_json(_persona_delete_request(body))



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
    description="HTTP endpoint handler for /ext/mode_roleplay/** (面板页+游戏页+数据面+写动作+游戏域通道)",
)
async def http_handle(
    path: str = "",
    method: str = "GET",
    plugin_id: str = "",
    raw_body: str = "",
    headers: dict[str, str] | None = None,
    query: dict[str, str] | None = None,
) -> dict[str, Any]:
    """按 path 分发：面板页 HTML / 数据端点 / 写动作；未路由返回 404 JSON。"""
    query = query or {}
    if path == _PANEL_ENDPOINT and method == "GET":
        try:
            return {"success": True, "data": _panel_html_response()}
        except OSError as exc:
            return {
                "success": False,
                "error": f"panel html read failed: {exc}",
                "data": _json_response({"error": "panel html unavailable"}, 500),
            }
    if path == _GAME_PAGE_ENDPOINT and method == "GET":
        try:
            return {"success": True, "data": _game_page_html_response()}
        except (OSError, RuntimeError) as exc:
            return {
                "success": False,
                "error": f"game page html unavailable: {exc}",
                "data": _json_response({"error": "game page html unavailable"}, 500),
            }
    if path == _GAME_STATE_ROUTE:
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        return await _handle_game_state(str(query.get("session_id") or ""))
    if path == _GAME_CONFIG_ROUTE:
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        return await _handle_game_config(str(query.get("session_id") or ""))
    if path == _GAME_PAGE_DATA_ROUTE:
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        # 双通道（刀4）：session_id=（刀1 契约零变化）| pack=（setup 通道，会话
        # 未开按包 id 直取页）；两参都缺 = 400（session 通道缺参语义同现状）。
        return await _handle_game_page(
            str(query.get("session_id") or ""), str(query.get("pack") or "")
        )
    if path in (_GAME_ATTACH_ROUTE, _GAME_SNAPSHOT_ROUTE, _GAME_DETACH_ROUTE, _GAME_TURN_ROUTE):
        if method != "POST":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        body = _parse_body(raw_body)
        if path == _GAME_ATTACH_ROUTE:
            return await _handle_game_attach(body)
        if path == _GAME_SNAPSHOT_ROUTE:
            return await _handle_game_snapshot(body)
        if path == _GAME_TURN_ROUTE:
            return await _handle_game_turn(body, headers)
        return await _handle_game_detach(body)
    if path == _GAME_OPERATION_ROUTE:
        # 自由操作→结构化命令（世界内嵌批次二）：Propose→Adjudicate，与 game/turn
        # 同闸（会话校验+单宿主租约）；独立分支不走 _ACTION_ROUTES（游戏域通道族）。
        if method != "POST":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        return await _handle_game_operation(_parse_body(raw_body))
    if path == _GAME_TICK_ROUTE:
        # 世界演化周期推进（世界内嵌批次三）：直达管道接口不经任务壳，与
        # game/operation 同闸（会话校验+单宿主租约）；游戏域通道族独立分支。
        if method != "POST":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        return await _handle_game_tick(_parse_body(raw_body))
    if path in _DATA_ROUTES and method == "GET":
        if path == _PRECIP_CANDIDATES_ROUTE:
            # 沉淀候选（缺口批次三）：会话审计流水复盘统计，读面无副作用；
            # session_id 必填（缺参 400 显式，与 messages 同款），未知会话/无
            # 流水 = 空列表（200，fail-soft 非错误）。
            session_id = str(query.get("session_id") or "").strip()
            if not session_id:
                return {"success": True, "data": _json_response({"error": "缺少 session_id"}, 400)}
            workspace = await _resolve_workspace(session_id)
            return {
                "success": True,
                "data": _json_response({
                    "candidates": precipitation_candidates(_precipitation_stream(workspace))
                }),
            }
        if path == _WIZARD_AXES_ROUTE:
            # 四轴候选集（C2 整合刀）：文风轴动态 + 三静态考据轴，读面无副作用
            return {"success": True, "data": _json_response({"axes": _wizard_axes_listing()})}
        if path == _PACKS_ROUTE:
            # 玩法包清点独立端点（面板数据驱动刀）：双根载荷（出厂∪用户层，同 id
            # 接管），fail-soft 目录缺失=空列表；读面无副作用。packs_dir 显式传
            # 全局（axes 的 _STYLES_DIR 同款：调用时读，测试可 monkeypatch 注入）。
            # RP 面板 v3.1：一并返回自定义玩法 combos（刀2 少一枪）。
            return {
                "success": True,
                "data": _json_response({
                    "packs": _load_packs_listing(packs_dir=_PACKS_DIR),
                    "combos": _load_combos(),
                }),
            }
        if path.endswith("/bootstrap"):
            profile = load_profile()
            payload = {
                "mode": profile["mode"],
                "name": profile["name"],
                "chain": profile.get("chain"),
                "panel_page_id": profile.get("panel_page_id"),
                # 玩法包清点（RP 面板刀-Ⅰ）：向导玩法勾选页数据源，聚合进既有
                # 端点零新增（出厂播种两件 + 用户层合并展示）
                "packs": _load_packs_listing(),
            }
            return {"success": True, "data": _json_response(payload)}
        if path.endswith("/sessions"):
            rows = await _call_provider("pipeline-state")
            sessions = _norm_sessions(rows)
            # ?pack=<id> 过滤（RP 面板 v3.1「继续上局」按玩法归属）：pack_ids
            # 命中即留；无过滤参数 = 全量（现状零变化）。
            pack_filter = str(query.get("pack") or "").strip()
            if pack_filter:
                sessions = [s for s in sessions if pack_filter in s["pack_ids"]]
            return {"success": True, "data": _json_response({"sessions": sessions})}
        if path.endswith("/cards"):
            return {"success": True, "data": _json_response({"cards": _load_cards()})}
        return {"success": True, "data": _json_response({"lorebooks": _load_lorebooks()})}
    if path == "/ext/mode_roleplay/data/personas":
        # GET 端点但在 _DATA_ROUTES 集合外——messages 端点同款独立分发形态
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        return {"success": True, "data": _json_response({"personas": _personas_load()})}
    if path == "/ext/mode_roleplay/data/messages":
        if method != "GET":
            return {"success": True, "data": _json_response({"error": "method not allowed"}, 404)}
        pipeline_id = str(query.get("pipeline_id") or "")
        if not pipeline_id:
            return {"success": True, "data": _json_response({"error": "缺少 pipeline_id"}, 400)}
        rows = await _call_provider("messages", pipeline_id=pipeline_id)
        return {"success": True, "data": _json_response({"messages": _norm_messages(rows)})}
    if path == _DRAFTS_ROUTE or path.startswith(_DRAFT_ROUTE_PREFIX):
        routed = await _handle_draft_routes(path, method, raw_body, query, headers)
        if routed is not None:
            return routed
    routed = await _handle_actions(path, method, raw_body)
    if routed is not None:
        return routed
    if path == "/ext/mode_roleplay/pipeline/open" and method == "POST":
        # B8 管道标签（2026-09-29）：向既有会话开通扮演子管道——薄代理内核
        # chat.send_message 创建分支（create:true + thread_id 归属既有会话，
        # 内核测试 create_with_ownership_thread_links_real_session 钉死）。
        # 职责就地（用户裁定）：面板直调本插件端点，不设独立代理插件。
        # body 解码走 _parse_body 单点（内核 base64 契约 + 裸 JSON 双形态，
        # 与 _handle_actions 同源）；非法可辨（_BODY_INVALID）→ 400 显式拒绝。
        req = _parse_body(raw_body, invalid=_BODY_INVALID)
        if req is _BODY_INVALID:
            return {"success": True, "data": _json_response({"error": "请求体非法 JSON"}, 400)}
        session_id = str(req.get("session_id", "") or "").strip()
        if not session_id:
            return {"success": True, "data": _json_response({"error": "session_id 必填"}, 400)}
        agent_id = str(req.get("agent_id", "") or "").strip()
        # user_id 从请求 token 解（内核转发 /ext 带全量 headers；token 载荷
        # `access:<uid>:<username>:<exp>:<jti>:<h>` 自包含 uid）——chat.send_message
        # 契约 user_id 非空，缺失即 401 如实拒绝（不猜默认用户）。
        user_id = _user_id_from_headers(headers)
        if not user_id:
            return {"success": True, "data": _json_response({"error": "未认证：token 缺失或无效"}, 401)}
        args: dict[str, Any] = {
            "create": True,
            "thread_id": session_id,
            "message": str(req.get("first_message", "") or ""),
            "user_id": user_id,
            "pipeline_config_id": "roleplay",
        }
        # 玩法级管道绑定（世界内嵌谱系右移批次一，调研 §6.4）：显式携带
        # pipeline_config_id 时过登记存在性闸——未登记名 fail-closed 回落缺省
        # roleplay + 响应 pipeline_warning（错误面可见），派发照常（会话出生
        # 绝不失败；内核对未知名本就回落空管道，闸门把回落收拢到契约缺省）；
        # 不携带 = 现役 roleplay 路径零变化。
        requested_pipeline = str(req.get("pipeline_config_id", "") or "").strip()
        pipeline_warning = ""
        if requested_pipeline and requested_pipeline in _registered_pipeline_names():
            args["pipeline_config_id"] = requested_pipeline
        elif requested_pipeline:
            pipeline_warning = (
                f"pipeline_config_id {requested_pipeline!r} 未登记"
                "（config/pipelines 无此名），回落缺省 roleplay"
            )
            logger.warning("[mode_roleplay] pipeline/open %s", pipeline_warning)
        if agent_id:
            # run 执行身份 = 管道 state 持久键 agent.id（出生方经 state 透传，
            # context_build 据此加载卡 agent yaml；顶层 agent_id 只是派发簿记）
            args["state"] = {"agent.id": agent_id}
        try:
            cap = plugin.get_capability("chat")
            from agentos_plugin_sdk.capability import bind_capability_caller

            res = await bind_capability_caller(cap, "chat")(
                "chat.send_message", args, timeout=25.0
            )
        except Exception as exc:  # noqa: BLE001 — 薄代理：capability 失败原样透传
            return {"success": True, "data": _json_response({"error": f"管道开通失败: {exc}"}, 502)}
        if not isinstance(res, dict) or res.get("status") != "created":
            return {
                "success": True,
                "data": _json_response({"error": f"未走创建分支: {str(res)[:200]}"}, 502),
            }
        pipeline_id = str(res.get("pipeline_id", "") or "")
        if not pipeline_id:
            return {"success": True, "data": _json_response({"error": "回执缺 pipeline_id"}, 502)}
        return {
            "success": True,
            "data": _json_response({
                "pipeline_id": pipeline_id,
                "thread_id": session_id,
                "status": "created",
                "pipeline_config_id": str(args["pipeline_config_id"]),
                # 键缺席式警示面：显式未登记名回落缺省时才带（bind 侧校验线，
                # 错误面可见——前端据此通知，派发本身不受阻）
                **({"pipeline_warning": pipeline_warning} if pipeline_warning else {}),
            }),
        }
    return {"success": True, "data": _json_response({"error": "not found", "path": path}, 404)}



if __name__ == "__main__":
    plugin.run()
