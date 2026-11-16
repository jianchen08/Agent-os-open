/**
 * 生成物：插件 http_endpoints 声明的投影 —— 勿手改！
 *
 * 唯一真值源 = 各插件 plugin.json 的 http_endpoints 声明
 * （ADR 2026-08-21 channel_api 退役：前端端点供给模型改生成式）。
 * 路径模板 {param} 原样保留，消费方自行替换参数。
 * 改动插件 manifest 后执行再生成：
 *     python scripts/gen_frontend_endpoints.py
 * 漂移/手写回潮检查：
 *     python scripts/check_frontend_endpoints_sync.py
 */
/* eslint-disable */


  /** acp_quota_pool（ACP Quota Pool）：plugin.json 声明 3 端点 */
  export const ACP_QUOTA_POOL_ENDPOINTS = {
    'acp_gateway_start': '/ext/acp_quota_pool/gateway/start',
    'acp_gateway_status': '/ext/acp_quota_pool/gateway/status',
    'acp_console_page': '/ext/acp_quota_pool/page/console',
  } as const

  /** agent_manager（Agent Manager）：plugin.json 声明 8 端点 */
  export const AGENT_MANAGER_ENDPOINTS = {
    'agent_manager_list': '/ext/agent_manager/agents',
    'agent_manager_schema': '/ext/agent_manager/agents/schema',
    'agent_manager_get_config': '/ext/agent_manager/agents/{id}/config',
    'agent_manager_put_config': '/ext/agent_manager/agents/{id}/config',
    'agent_manager_prompt_preview': '/ext/agent_manager/agents/{id}/prompt-preview',
    'agent_manager_mode_get_config': '/ext/agent_manager/agents/{mode_id}/{stem}/config',
    'agent_manager_mode_put_config': '/ext/agent_manager/agents/{mode_id}/{stem}/config',
    'agent_manager_modes': '/ext/agent_manager/modes',
  } as const

  /** approval_service（Approval Service）：plugin.json 声明 7 端点 */
  export const APPROVAL_SERVICE_ENDPOINTS = {
    'interaction_pending': '/ext/approval_service/interaction/pending',
    'interaction_response': '/ext/approval_service/interaction/response',
    'interaction_get': '/ext/approval_service/interaction/{request_id}',
    'interaction_approve': '/ext/approval_service/interaction/{request_id}/approve',
    'interaction_cancel': '/ext/approval_service/interaction/{request_id}/cancel',
    'interaction_deny': '/ext/approval_service/interaction/{request_id}/deny',
    'interaction_viewed': '/ext/approval_service/interaction/{request_id}/viewed',
  } as const

  /** artifacts（Artifacts Service）：plugin.json 声明 14 端点 */
  export const ARTIFACTS_ENDPOINTS = {
    'artifacts_create': '/ext/artifacts',
    'artifacts_list': '/ext/artifacts',
    'annotations_delete': '/ext/artifacts/annotations/{annotation_id}',
    'annotations_update': '/ext/artifacts/annotations/{annotation_id}',
    'annotations_resolve': '/ext/artifacts/annotations/{annotation_id}/resolve',
    'artifacts_file_content': '/ext/artifacts/files/{stored_filename}',
    'artifacts_upload': '/ext/artifacts/upload',
    'artifacts_delete': '/ext/artifacts/{artifact_id}',
    'artifacts_get': '/ext/artifacts/{artifact_id}',
    'artifacts_update': '/ext/artifacts/{artifact_id}',
    'artifacts_annotations_create': '/ext/artifacts/{artifact_id}/annotations',
    'artifacts_annotations_list': '/ext/artifacts/{artifact_id}/annotations',
    'artifacts_diff': '/ext/artifacts/{artifact_id}/diff',
    'artifacts_versions': '/ext/artifacts/{artifact_id}/versions',
  } as const

  /** browser_tool（Browser Tool）：plugin.json 声明 2 端点 */
  export const BROWSER_TOOL_ENDPOINTS = {
    'browser_live_ticket': '/ext/browser_tool/live',
    'browser_live_page': '/ext/browser_tool/page/browser-live',
  } as const

  /** channel_wecom（WeCom Channel）：plugin.json 声明 2 端点 */
  export const CHANNEL_WECOM_ENDPOINTS = {
    'wecom_callback': '/ext/channel_wecom/callback',
    'wecom_verify': '/ext/channel_wecom/callback',
  } as const

  /** cost_control（Cost Control Service）：plugin.json 声明 7 端点 */
  export const COST_CONTROL_ENDPOINTS = {
    'cost_budget_reset': '/ext/cost_control/budget/reset',
    'cost_budget_status': '/ext/cost_control/budget/status',
    'cost_config': '/ext/cost_control/config',
    'cost_config_file_get': '/ext/cost_control/config/cost-control',
    'cost_config_file_put': '/ext/cost_control/config/cost-control',
    'cost_report': '/ext/cost_control/report',
    'cost_usage_statistics': '/ext/cost_control/usage/statistics',
  } as const

  /** db_admin（DB Admin HTTP Face）：plugin.json 声明 7 端点 */
  export const DB_ADMIN_ENDPOINTS = {
    'execute': '/ext/db_admin/execute',
    'table_insert': '/ext/db_admin/table/{table}',
    'table_rows': '/ext/db_admin/table/{table}',
    'table_row': '/ext/db_admin/table/{table}/{pk_value}',
    'table_row_delete': '/ext/db_admin/table/{table}/{pk_value}',
    'table_row_update': '/ext/db_admin/table/{table}/{pk_value}',
    'tables': '/ext/db_admin/tables',
  } as const

  /** dsh_adapter（DSH Plugin Adapter）：plugin.json 声明 16 端点 */
  export const DSH_ADAPTER_ENDPOINTS = {
    'dsh_hooks_translate': '/ext/dsh_adapter/api/dsh/hooks/translate',
    'dsh_plugins_list': '/ext/dsh_adapter/api/dsh/plugins',
    'dsh_plugins_disable': '/ext/dsh_adapter/api/dsh/plugins/disable',
    'dsh_plugins_enable': '/ext/dsh_adapter/api/dsh/plugins/enable',
    'dsh_plugins_index': '/ext/dsh_adapter/api/dsh/plugins/index',
    'dsh_plugins_install': '/ext/dsh_adapter/api/dsh/plugins/install',
    'dsh_plugins_uninstall': '/ext/dsh_adapter/api/dsh/plugins/uninstall',
    'dsh_manager_page': '/ext/dsh_adapter/page/manager',
    'dsh_skin_assets': '/ext/dsh_adapter/styles/skin-assets/{skin}/{file:path}',
    'dsh_skin_hooks': '/ext/dsh_adapter/styles/skin/{skin}/hooks.mjs',
    'dsh_skin_merged_css': '/ext/dsh_adapter/styles/skin/{skin}/merged.css',
    'dsh_web_proxy_delete': '/ext/dsh_adapter/web/{path:path}',
    'dsh_web_proxy_get': '/ext/dsh_adapter/web/{path:path}',
    'dsh_web_proxy_patch': '/ext/dsh_adapter/web/{path:path}',
    'dsh_web_proxy_post': '/ext/dsh_adapter/web/{path:path}',
    'dsh_web_proxy_put': '/ext/dsh_adapter/web/{path:path}',
  } as const

  /** e2e_lifecycle_probe（E2E Lifecycle Probe）：plugin.json 声明 1 端点 */
  export const E2E_LIFECYCLE_PROBE_ENDPOINTS = {
    'probe_echo': '/ext/e2e_lifecycle_probe/echo',
  } as const

  /** evaluation_service（Evaluation Service）：plugin.json 声明 3 端点 */
  export const EVALUATION_SERVICE_ENDPOINTS = {
    'metrics_list': '/ext/evaluation_service/metrics',
    'metric_delete': '/ext/evaluation_service/metrics/{metric_id}',
    'metric_detail': '/ext/evaluation_service/metrics/{metric_id}',
  } as const

  /** hindsight_memory_service（Hindsight Memory Service）：plugin.json 声明 20 端点 */
  export const HINDSIGHT_MEMORY_SERVICE_ENDPOINTS = {
    'kb_list': '/ext/hindsight_memory_service/knowledge-base',
    'kb_check': '/ext/hindsight_memory_service/knowledge-base/check',
    'kb_search': '/ext/hindsight_memory_service/knowledge-base/search',
    'kb_stats': '/ext/hindsight_memory_service/knowledge-base/stats',
    'kb_tags': '/ext/hindsight_memory_service/knowledge-base/tags',
    'kb_upload': '/ext/hindsight_memory_service/knowledge-base/upload',
    'kb_item_delete': '/ext/hindsight_memory_service/knowledge-base/{item_id}',
    'kb_item_get': '/ext/hindsight_memory_service/knowledge-base/{item_id}',
    'memory_list': '/ext/hindsight_memory_service/memory',
    'memory_consolidate': '/ext/hindsight_memory_service/memory/consolidate',
    'memory_episodes_list': '/ext/hindsight_memory_service/memory/episodes',
    'memory_episode_get': '/ext/hindsight_memory_service/memory/episodes/{episode_id}',
    'memory_search_get': '/ext/hindsight_memory_service/memory/search',
    'memory_search_post': '/ext/hindsight_memory_service/memory/search',
    'memory_semantic_list': '/ext/hindsight_memory_service/memory/semantic',
    'memory_stats': '/ext/hindsight_memory_service/memory/stats',
    'memory_delete': '/ext/hindsight_memory_service/memory/{memory_id}',
    'memory_get': '/ext/hindsight_memory_service/memory/{memory_id}',
    'hindsight_recall': '/ext/hindsight_memory_service/recall',
    'hindsight_stats': '/ext/hindsight_memory_service/stats',
  } as const

  /** llm_service（LLM Service）：plugin.json 声明 10 端点 */
  export const LLM_SERVICE_ENDPOINTS = {
    'config_llm_presets_get': '/ext/llm_service/config/llm/presets',
    'config_llm_provider_types_get': '/ext/llm_service/config/llm/provider-types',
    'config_llm_providers_remote_models_get': '/ext/llm_service/config/llm/providers/{provider_id}/remote-models',
    'config_llm_thinking_levels': '/ext/llm_service/config/llm/thinking-levels',
    'thinking_mode_check': '/ext/llm_service/thinking-mode/check/{model_name}',
    'thinking_mode_health': '/ext/llm_service/thinking-mode/healthz',
    'thinking_mode_models_list': '/ext/llm_service/thinking-mode/models',
    'thinking_mode_model_info': '/ext/llm_service/thinking-mode/models/{model_name}',
    'thinking_mode_recommendations': '/ext/llm_service/thinking-mode/recommendations',
    'thinking_mode_switch': '/ext/llm_service/thinking-mode/switch',
  } as const

  /** metrics_admin（Metrics Admin HTTP Face）：plugin.json 声明 3 端点 */
  export const METRICS_ADMIN_ENDPOINTS = {
    'prometheus': '/ext/metrics_admin/prometheus',
    'query': '/ext/metrics_admin/query',
    'series': '/ext/metrics_admin/series',
  } as const

  /** mode_coding（Mode Coding）：plugin.json 声明 7 端点 */
  export const MODE_CODING_ENDPOINTS = {
    'mode_coding_action_dispatch_issue': '/ext/mode_coding/data/actions/dispatch_issue',
    'mode_coding_data_board': '/ext/mode_coding/data/board',
    'mode_coding_data_bootstrap': '/ext/mode_coding/data/bootstrap',
    'mode_coding_data_messages': '/ext/mode_coding/data/messages',
    'mode_coding_data_reviews': '/ext/mode_coding/data/reviews',
    'mode_coding_data_sessions': '/ext/mode_coding/data/sessions',
    'mode_coding_panel_page': '/ext/mode_coding/page/coding-panel',
  } as const

  /** mode_evolution（Mode Evolution）：plugin.json 声明 1 端点 */
  export const MODE_EVOLUTION_ENDPOINTS = {
    'mode_evolution_panel_page': '/ext/mode_evolution/page/evolution-panel',
  } as const

  /** mode_godot（Mode Godot）：plugin.json 声明 9 端点 */
  export const MODE_GODOT_ENDPOINTS = {
    'mode_godot_action_dispatch': '/ext/mode_godot/data/actions/dispatch',
    'mode_godot_action_open_editor': '/ext/mode_godot/data/actions/open_editor',
    'mode_godot_data_bootstrap': '/ext/mode_godot/data/bootstrap',
    'mode_godot_data_messages': '/ext/mode_godot/data/messages',
    'mode_godot_data_preview': '/ext/mode_godot/data/preview',
    'mode_godot_data_records': '/ext/mode_godot/data/records',
    'mode_godot_data_scene': '/ext/mode_godot/data/scene',
    'mode_godot_data_sessions': '/ext/mode_godot/data/sessions',
    'mode_godot_panel_page': '/ext/mode_godot/page/godot-panel',
  } as const

  /** mode_personal（Mode Personal）：plugin.json 声明 20 端点 */
  export const MODE_PERSONAL_ENDPOINTS = {
    'personal_pim_capture': '/ext/mode_personal/capture',
    'personal_pim_events_create': '/ext/mode_personal/events',
    'personal_pim_events_list': '/ext/mode_personal/events',
    'personal_pim_event_patch': '/ext/mode_personal/events/{id}',
    'personal_pim_focus_start': '/ext/mode_personal/focus/{id}/start',
    'personal_pim_focus_stop': '/ext/mode_personal/focus/{id}/stop',
    'personal_pim_habits_create': '/ext/mode_personal/habits',
    'personal_pim_habits_list': '/ext/mode_personal/habits',
    'personal_pim_habit_patch': '/ext/mode_personal/habits/{id}',
    'personal_pim_habit_checkin': '/ext/mode_personal/habits/{id}/checkin',
    'personal_pim_today_page': '/ext/mode_personal/page/pim-today',
    'personal_pim_partner': '/ext/mode_personal/partner',
    'personal_pim_today': '/ext/mode_personal/today',
    'personal_pim_todos_create': '/ext/mode_personal/todos',
    'personal_pim_todos_list': '/ext/mode_personal/todos',
    'personal_pim_todo_patch': '/ext/mode_personal/todos/{id}',
    'personal_pim_todo_complete': '/ext/mode_personal/todos/{id}/complete',
    'personal_pim_todo_defer': '/ext/mode_personal/todos/{id}/defer',
    'personal_pim_todo_split': '/ext/mode_personal/todos/{id}/split',
    'personal_pim_undo': '/ext/mode_personal/undo/{op_id}',
  } as const

  /** mode_research（Mode Research）：plugin.json 声明 8 端点 */
  export const MODE_RESEARCH_ENDPOINTS = {
    'mode_research_action_followup': '/ext/mode_research/data/actions/followup',
    'mode_research_action_start': '/ext/mode_research/data/actions/start',
    'mode_research_data_bootstrap': '/ext/mode_research/data/bootstrap',
    'mode_research_data_messages': '/ext/mode_research/data/messages',
    'mode_research_data_report': '/ext/mode_research/data/report',
    'mode_research_data_sessions': '/ext/mode_research/data/sessions',
    'mode_research_data_sources': '/ext/mode_research/data/sources',
    'mode_research_panel_page': '/ext/mode_research/page/research-panel',
  } as const

  /** mode_roleplay（Mode Roleplay）：plugin.json 声明 43 端点 */
  export const MODE_ROLEPLAY_ENDPOINTS = {
    'mode_roleplay_action_play': '/ext/mode_roleplay/data/actions/play',
    'mode_roleplay_action_regenerate': '/ext/mode_roleplay/data/actions/regenerate',
    'mode_roleplay_data_bootstrap': '/ext/mode_roleplay/data/bootstrap',
    'mode_roleplay_data_cards': '/ext/mode_roleplay/data/cards',
    'mode_roleplay_card_delete': '/ext/mode_roleplay/data/cards/delete',
    'mode_roleplay_card_export': '/ext/mode_roleplay/data/cards/export',
    'mode_roleplay_card_import': '/ext/mode_roleplay/data/cards/import',
    'mode_roleplay_card_save': '/ext/mode_roleplay/data/cards/save',
    'mode_roleplay_combo_delete': '/ext/mode_roleplay/data/combos/delete',
    'mode_roleplay_combo_save': '/ext/mode_roleplay/data/combos/save',
    'mode_roleplay_scene_turn': '/ext/mode_roleplay/data/group/scene_turn',
    'mode_roleplay_data_lorebooks': '/ext/mode_roleplay/data/lorebooks',
    'mode_roleplay_lorebook_delete': '/ext/mode_roleplay/data/lorebooks/delete',
    'mode_roleplay_lorebook_import': '/ext/mode_roleplay/data/lorebooks/import',
    'mode_roleplay_lorebook_save': '/ext/mode_roleplay/data/lorebooks/save',
    'mode_roleplay_data_messages': '/ext/mode_roleplay/data/messages',
    'mode_roleplay_data_packs': '/ext/mode_roleplay/data/packs',
    'mode_roleplay_data_personas': '/ext/mode_roleplay/data/personas',
    'mode_roleplay_persona_delete': '/ext/mode_roleplay/data/personas/delete',
    'mode_roleplay_persona_save': '/ext/mode_roleplay/data/personas/save',
    'mode_roleplay_precipitation_candidates': '/ext/mode_roleplay/data/precipitation/candidates',
    'mode_roleplay_precipitation_promote': '/ext/mode_roleplay/data/precipitation/promote',
    'mode_roleplay_data_sessions': '/ext/mode_roleplay/data/sessions',
    'mode_roleplay_data_wizard_axes': '/ext/mode_roleplay/data/wizard/axes',
    'mode_roleplay_draft_approve': '/ext/mode_roleplay/data/wizards/draft/approve',
    'mode_roleplay_draft_lint': '/ext/mode_roleplay/data/wizards/draft/lint',
    'mode_roleplay_draft_register': '/ext/mode_roleplay/data/wizards/draft/register',
    'mode_roleplay_draft_reject': '/ext/mode_roleplay/data/wizards/draft/reject',
    'mode_roleplay_draft_smoke': '/ext/mode_roleplay/data/wizards/draft/smoke',
    'mode_roleplay_draft_submit': '/ext/mode_roleplay/data/wizards/draft/submit',
    'mode_roleplay_drafts_list': '/ext/mode_roleplay/data/wizards/drafts',
    'mode_roleplay_game_config': '/ext/mode_roleplay/game/config',
    'mode_roleplay_game_detach': '/ext/mode_roleplay/game/detach',
    'mode_roleplay_game_operation': '/ext/mode_roleplay/game/operation',
    'mode_roleplay_game_page_data': '/ext/mode_roleplay/game/page',
    'mode_roleplay_game_attach': '/ext/mode_roleplay/game/session/attach',
    'mode_roleplay_game_snapshot': '/ext/mode_roleplay/game/snapshot',
    'mode_roleplay_game_state': '/ext/mode_roleplay/game/state',
    'mode_roleplay_game_tick': '/ext/mode_roleplay/game/tick',
    'mode_roleplay_game_turn': '/ext/mode_roleplay/game/turn',
    'mode_roleplay_game_page': '/ext/mode_roleplay/page/game-host',
    'mode_roleplay_panel_page': '/ext/mode_roleplay/page/roleplay-panel',
    'pipeline_open': '/ext/mode_roleplay/pipeline/open',
  } as const

  /** mode_writing（Mode Writing）：plugin.json 声明 8 端点 */
  export const MODE_WRITING_ENDPOINTS = {
    'mode_writing_action_chapter_act': '/ext/mode_writing/data/actions/chapter_act',
    'mode_writing_data_bible': '/ext/mode_writing/data/bible',
    'mode_writing_data_bootstrap': '/ext/mode_writing/data/bootstrap',
    'mode_writing_data_chapter': '/ext/mode_writing/data/chapter',
    'mode_writing_data_messages': '/ext/mode_writing/data/messages',
    'mode_writing_data_sessions': '/ext/mode_writing/data/sessions',
    'mode_writing_data_works': '/ext/mode_writing/data/works',
    'mode_writing_panel_page': '/ext/mode_writing/page/writing-panel',
  } as const

  /** monitoring（Monitoring Service）：plugin.json 声明 28 端点 */
  export const MONITORING_ENDPOINTS = {
    'mon_cache_stats': '/ext/monitoring/cache-stats',
    'mon_diagnostics_settings_get': '/ext/monitoring/diagnostics-settings',
    'mon_diagnostics_settings_put': '/ext/monitoring/diagnostics-settings',
    'mon_execution_records_list': '/ext/monitoring/execution/records',
    'mon_execution_records_clear_all': '/ext/monitoring/execution/records/clear-all',
    'mon_execution_records_group_summary': '/ext/monitoring/execution/records/group-summary',
    'mon_execution_records_sessions': '/ext/monitoring/execution/records/sessions',
    'mon_execution_records_tree': '/ext/monitoring/execution/records/tree/{session_id}',
    'mon_execution_record_get': '/ext/monitoring/execution/records/{record_id}',
    'mon_execution_record_children': '/ext/monitoring/execution/records/{record_id}/children',
    'mon_orphan_runs': '/ext/monitoring/orphans',
    'mon_tool_calls_page': '/ext/monitoring/page/tool-calls',
    'mon_payload_diag_list': '/ext/monitoring/payload-diag',
    'mon_payload_diag_get': '/ext/monitoring/payload-diag/file',
    'mon_pipeline_state_full': '/ext/monitoring/pipeline-state',
    'mon_plugin_health': '/ext/monitoring/plugin-health',
    'mon_plugins_runtime': '/ext/monitoring/plugins',
    'mon_search_global': '/ext/monitoring/search',
    'mon_sessions_context_token_usage': '/ext/monitoring/sessions/{session_id}/context-token-usage',
    'mon_sessions_total_token_usage': '/ext/monitoring/sessions/{session_id}/total-token-usage',
    'mon_system_metrics': '/ext/monitoring/system/metrics',
    'mon_tasks': '/ext/monitoring/tasks',
    'mon_task_statistics': '/ext/monitoring/tasks/statistics',
    'mon_token_usage': '/ext/monitoring/token-usage',
    'mon_token_usage_by_time': '/ext/monitoring/token-usage/by-time',
    'mon_tool_calls': '/ext/monitoring/tool-calls',
    'mon_tool_calls_stats': '/ext/monitoring/tool-calls/stats',
    'mon_traces_by_pipeline': '/ext/monitoring/traces',
  } as const

  /** multimodal_service（Multimodal Service）：plugin.json 声明 3 端点 */
  export const MULTIMODAL_SERVICE_ENDPOINTS = {
    'mm_asr_transcriptions': '/ext/multimodal_service/audio/transcriptions',
    'mm_files_capabilities': '/ext/multimodal_service/files/capabilities',
    'mm_files_supported_types': '/ext/multimodal_service/files/supported-types',
  } as const

  /** notification_sounds（Notification Sounds）：plugin.json 声明 1 端点 */
  export const NOTIFICATION_SOUNDS_ENDPOINTS = {
    'sounds_catalog': '/ext/notification_sounds/sounds',
  } as const

  /** oauth_broker（OAuth Broker）：plugin.json 声明 1 端点 */
  export const OAUTH_BROKER_ENDPOINTS = {
    'oauth_callback': '/ext/oauth_broker/callback',
  } as const

  /** onboarding_service（Onboarding Service）：plugin.json 声明 3 端点 */
  export const ONBOARDING_SERVICE_ENDPOINTS = {
    'progress_get': '/ext/onboarding_service/progress',
    'progress_update': '/ext/onboarding_service/progress',
    'walkthroughs_list': '/ext/onboarding_service/walkthroughs',
  } as const

  /** pipeline_host_context（Host Context）：plugin.json 声明 5 端点 */
  export const PIPELINE_HOST_CONTEXT_ENDPOINTS = {
    'selection_preview': '/ext/pipeline_host_context/preview',
    'selection_clear': '/ext/pipeline_host_context/selection',
    'selection_push': '/ext/pipeline_host_context/selection',
    'selection_snapshot': '/ext/pipeline_host_context/selection',
    'selection_subscribe': '/ext/pipeline_host_context/subscribe',
  } as const

  /** pipeline_security_check（Security Check）：plugin.json 声明 5 端点 */
  export const PIPELINE_SECURITY_CHECK_ENDPOINTS = {
    'permission_mode_get': '/ext/pipeline_security_check/permission_mode',
    'permission_mode_switch': '/ext/pipeline_security_check/permission_mode',
    'zone_policy_get': '/ext/pipeline_security_check/zones',
    'zone_policy_add': '/ext/pipeline_security_check/zones/add',
    'zone_policy_remove': '/ext/pipeline_security_check/zones/remove',
  } as const

  /** pipeline_state_marker_parse（State Marker Parse）：plugin.json 声明 1 端点 */
  export const PIPELINE_STATE_MARKER_PARSE_ENDPOINTS = {
    'state_marker_latest': '/ext/pipeline_state_marker_parse/latest',
  } as const

  /** pipeline_todo_track（Todo Track）：plugin.json 声明 2 端点 */
  export const PIPELINE_TODO_TRACK_ENDPOINTS = {
    'todo_latest': '/ext/pipeline_todo_track/latest',
    'todo_plan_page': '/ext/pipeline_todo_track/page/todo-plan',
  } as const

  /** review_service（Review Service）：plugin.json 声明 9 端点 */
  export const REVIEW_SERVICE_ENDPOINTS = {
    'review_create': '/ext/review_service/reviews',
    'review_list': '/ext/review_service/reviews',
    'review_media_review': '/ext/review_service/reviews/media-review',
    'review_get': '/ext/review_service/reviews/{review_id}',
    'review_attachments': '/ext/review_service/reviews/{review_id}/attachments',
    'review_cancel': '/ext/review_service/reviews/{review_id}/cancel',
    'review_feedback': '/ext/review_service/reviews/{review_id}/feedback',
    'review_media_metadata': '/ext/review_service/reviews/{review_id}/media-metadata',
    'review_viewed': '/ext/review_service/reviews/{review_id}/viewed',
  } as const

  /** scene_service（Scene Service）：plugin.json 声明 7 端点 */
  export const SCENE_SERVICE_ENDPOINTS = {
    'scenes_create': '/ext/scene_service/scenes',
    'scenes_list': '/ext/scene_service/scenes',
    'scenes_templates': '/ext/scene_service/scenes/templates',
    'scene_delete': '/ext/scene_service/scenes/{scene_id}',
    'scene_get': '/ext/scene_service/scenes/{scene_id}',
    'scene_update': '/ext/scene_service/scenes/{scene_id}',
    'scene_switch': '/ext/scene_service/scenes/{scene_id}/switch',
  } as const

  /** snippet_box（SnippetBox）：plugin.json 声明 1 端点 */
  export const SNIPPET_BOX_ENDPOINTS = {
    'snippet_box_page': '/ext/snippet_box/page/snippet_box',
  } as const

  /** sync_agent（Sync Agent）：plugin.json 声明 11 端点 */
  export const SYNC_AGENT_ENDPOINTS = {
    'backup_points': '/ext/sync_agent/backup/points',
    'backup_restore': '/ext/sync_agent/backup/restore',
    'backup_run': '/ext/sync_agent/backup/run',
    'backup_status': '/ext/sync_agent/backup/status',
    'borrow': '/ext/sync_agent/borrow',
    'borrow_auth': '/ext/sync_agent/borrow_auth',
    'enroll_device': '/ext/sync_agent/enroll',
    'mirror_records': '/ext/sync_agent/mirror/records',
    'sync_probe': '/ext/sync_agent/probe',
    'relay_devices': '/ext/sync_agent/relay/devices',
    'sync_status': '/ext/sync_agent/status',
  } as const

  /** sync_relay（Sync Relay）：plugin.json 声明 14 端点 */
  export const SYNC_RELAY_ENDPOINTS = {
    'bootstrap_baseline': '/ext/sync_relay/bootstrap',
    'borrow_auth': '/ext/sync_relay/borrow_auth',
    'list_dead_letters': '/ext/sync_relay/dead_letters',
    'list_devices': '/ext/sync_relay/devices',
    'register_device': '/ext/sync_relay/devices',
    'device_heartbeat': '/ext/sync_relay/devices/heartbeat',
    'enroll_device': '/ext/sync_relay/enroll',
    'pull_events': '/ext/sync_relay/events',
    'receive_events': '/ext/sync_relay/events',
    'exec_verification_set': '/ext/sync_relay/exec_tokens',
    'issue_exec_token': '/ext/sync_relay/exec_tokens',
    'registry': '/ext/sync_relay/registry',
    'routing_view': '/ext/sync_relay/routing_view',
    'verify_device': '/ext/sync_relay/verify',
  } as const

  /** task_form（Task Form Service）：plugin.json 声明 3 端点 */
  export const TASK_FORM_ENDPOINTS = {
    'task_form_get': '/ext/task_form/form',
    'task_form_agents': '/ext/task_form/options/agents',
    'task_form_projects': '/ext/task_form/options/projects',
  } as const

  /** task_service（Task Service）：plugin.json 声明 27 端点 */
  export const TASK_SERVICE_ENDPOINTS = {
    'projects_create': '/ext/task_service/projects',
    'projects_list': '/ext/task_service/projects',
    'project_delete': '/ext/task_service/projects/{project_id}',
    'project_get': '/ext/task_service/projects/{project_id}',
    'project_toggle_auto_execute': '/ext/task_service/projects/{project_id}/auto-execute',
    'project_pause': '/ext/task_service/projects/{project_id}/pause',
    'project_resume': '/ext/task_service/projects/{project_id}/resume',
    'tasks_create': '/ext/task_service/tasks',
    'tasks_list': '/ext/task_service/tasks',
    'tasks_debug_all': '/ext/task_service/tasks/debug/all',
    'tasks_create_root': '/ext/task_service/tasks/root',
    'task_delete': '/ext/task_service/tasks/{task_id}',
    'task_get': '/ext/task_service/tasks/{task_id}',
    'task_update': '/ext/task_service/tasks/{task_id}',
    'task_ac_list': '/ext/task_service/tasks/{task_id}/ac',
    'task_ac_evaluate_all': '/ext/task_service/tasks/{task_id}/ac/evaluate-all',
    'task_ac_evaluate': '/ext/task_service/tasks/{task_id}/ac/{ac_id}/evaluate',
    'task_ac_result': '/ext/task_service/tasks/{task_id}/ac/{ac_id}/result',
    'task_cancel': '/ext/task_service/tasks/{task_id}/cancel',
    'task_evaluate': '/ext/task_service/tasks/{task_id}/evaluate',
    'task_pause': '/ext/task_service/tasks/{task_id}/pause',
    'task_phase_status': '/ext/task_service/tasks/{task_id}/phase',
    'task_phase_complete_execute': '/ext/task_service/tasks/{task_id}/phase/execute/complete',
    'task_phase_complete_prepare': '/ext/task_service/tasks/{task_id}/phase/prepare/complete',
    'task_phase_output': '/ext/task_service/tasks/{task_id}/phase/{phase}/output',
    'task_resume': '/ext/task_service/tasks/{task_id}/resume',
    'task_submit': '/ext/task_service/tasks/{task_id}/submit',
  } as const

  /** trace_ingestion（Trace Ingestion）：plugin.json 声明 3 端点 */
  export const TRACE_INGESTION_ENDPOINTS = {
    'list_dead_letters': '/ext/trace_ingestion/dead_letters',
    'ingest_events': '/ext/trace_ingestion/events',
    'query_events': '/ext/trace_ingestion/events',
  } as const

  /** trigger_setup_tool（Trigger Setup Tool）：plugin.json 声明 10 端点 */
  export const TRIGGER_SETUP_TOOL_ENDPOINTS = {
    'trigger_pipelines': '/ext/trigger_setup_tool/pipelines',
    'trigger_create': '/ext/trigger_setup_tool/triggers',
    'trigger_list': '/ext/trigger_setup_tool/triggers',
    'trigger_stats': '/ext/trigger_setup_tool/triggers/stats',
    'trigger_delete': '/ext/trigger_setup_tool/triggers/{trigger_id}',
    'trigger_get': '/ext/trigger_setup_tool/triggers/{trigger_id}',
    'trigger_update': '/ext/trigger_setup_tool/triggers/{trigger_id}',
    'trigger_disable': '/ext/trigger_setup_tool/triggers/{trigger_id}/disable',
    'trigger_enable': '/ext/trigger_setup_tool/triggers/{trigger_id}/enable',
    'trigger_fire': '/ext/trigger_setup_tool/triggers/{trigger_id}/trigger',
  } as const

  /** user_admin（User Admin HTTP Face）：plugin.json 声明 10 端点 */
  export const USER_ADMIN_ENDPOINTS = {
    'users': '/ext/user_admin/users',
    'user_settings_get': '/ext/user_admin/users/settings',
    'user_settings_put': '/ext/user_admin/users/settings',
    'users_stats': '/ext/user_admin/users/stats',
    'user_delete': '/ext/user_admin/users/{user_id}',
    'user_active_update': '/ext/user_admin/users/{user_id}/active',
    'user_active_update_patch': '/ext/user_admin/users/{user_id}/active',
    'user_role_update': '/ext/user_admin/users/{user_id}/role',
    'user_role_update_put': '/ext/user_admin/users/{user_id}/role',
    'user_tenant_update': '/ext/user_admin/users/{user_id}/tenant',
  } as const

  /** workspace_service（Workspace Service）：plugin.json 声明 12 端点 */
  export const WORKSPACE_SERVICE_ENDPOINTS = {
    'workspaces_fs_tree': '/ext/workspace_service/workspaces/fs-tree',
    'workspaces_open_file': '/ext/workspace_service/workspaces/open-file',
    'workspaces_get': '/ext/workspace_service/workspaces/{container_task_id}',
    'workspaces_artifacts': '/ext/workspace_service/workspaces/{container_task_id}/artifacts',
    'workspaces_create_entry': '/ext/workspace_service/workspaces/{container_task_id}/create-entry',
    'workspaces_delete_entry': '/ext/workspace_service/workspaces/{container_task_id}/entries',
    'workspaces_file_content_get': '/ext/workspace_service/workspaces/{container_task_id}/file-content',
    'workspaces_file_content_put': '/ext/workspace_service/workspaces/{container_task_id}/file-content',
    'workspaces_file_tree': '/ext/workspace_service/workspaces/{container_task_id}/file-tree',
    'workspaces_move_entry': '/ext/workspace_service/workspaces/{container_task_id}/move-entry',
    'workspaces_open': '/ext/workspace_service/workspaces/{container_task_id}/open',
    'workspaces_rename_entry': '/ext/workspace_service/workspaces/{container_task_id}/rename-entry',
  } as const

  /** 模式包呈现数据端点按插件聚合（路径以 /data/cards 结尾的声明投影，未声明即无此面） */
  export const MODE_DATA_CARDS_ENDPOINTS: Record<string, string> = {
    'mode_roleplay': '/ext/mode_roleplay/data/cards',
  }
