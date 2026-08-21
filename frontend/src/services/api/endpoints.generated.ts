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


  /** agent_manager（Agent Manager）：plugin.json 声明 4 端点 */
  export const AGENT_MANAGER_ENDPOINTS = {
    'agent_manager_list': '/ext/agent_manager/agents',
    'agent_manager_schema': '/ext/agent_manager/agents/schema',
    'agent_manager_get_config': '/ext/agent_manager/agents/{id}/config',
    'agent_manager_put_config': '/ext/agent_manager/agents/{id}/config',
  } as const

  /** artifacts（Artifacts Service）：plugin.json 声明 13 端点 */
  export const ARTIFACTS_ENDPOINTS = {
    'artifacts_create': '/ext/artifacts',
    'artifacts_list': '/ext/artifacts',
    'annotations_delete': '/ext/artifacts/annotations/{annotation_id}',
    'annotations_update': '/ext/artifacts/annotations/{annotation_id}',
    'annotations_resolve': '/ext/artifacts/annotations/{annotation_id}/resolve',
    'artifacts_upload': '/ext/artifacts/upload',
    'artifacts_delete': '/ext/artifacts/{artifact_id}',
    'artifacts_get': '/ext/artifacts/{artifact_id}',
    'artifacts_update': '/ext/artifacts/{artifact_id}',
    'artifacts_annotations_create': '/ext/artifacts/{artifact_id}/annotations',
    'artifacts_annotations_list': '/ext/artifacts/{artifact_id}/annotations',
    'artifacts_diff': '/ext/artifacts/{artifact_id}/diff',
    'artifacts_versions': '/ext/artifacts/{artifact_id}/versions',
  } as const

  /** channel_wecom（WeCom Channel）：plugin.json 声明 2 端点 */
  export const CHANNEL_WECOM_ENDPOINTS = {
    'wecom_callback': '/ext/channel_wecom/callback',
    'wecom_verify': '/ext/channel_wecom/callback',
  } as const

  /** cost_control（Cost Control Service）：plugin.json 声明 5 端点 */
  export const COST_CONTROL_ENDPOINTS = {
    'cost_budget_reset': '/ext/cost_control/budget/reset',
    'cost_budget_status': '/ext/cost_control/budget/status',
    'cost_config': '/ext/cost_control/config',
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

  /** demo_widget_plugin（Demo Widget Plugin）：plugin.json 声明 1 端点 */
  export const DEMO_WIDGET_PLUGIN_ENDPOINTS = {
    'webview': '/ext/demo_widget_plugin/webview',
  } as const

  /** dsh_adapter（DSH Plugin Adapter）：plugin.json 声明 4 端点 */
  export const DSH_ADAPTER_ENDPOINTS = {
    'dsh_skin_list': '/ext/dsh_adapter/skins',
    'dsh_skin_select': '/ext/dsh_adapter/skins/current',
    'dsh_skin_assets': '/ext/dsh_adapter/styles/skin-assets/{skin}/{file:path}',
    'dsh_skin_css': '/ext/dsh_adapter/styles/skin.css',
  } as const

  /** evaluation_service（Evaluation Service）：plugin.json 声明 3 端点 */
  export const EVALUATION_SERVICE_ENDPOINTS = {
    'metrics_list': '/ext/evaluation_service/metrics',
    'metric_delete': '/ext/evaluation_service/metrics/{metric_id}',
    'metric_detail': '/ext/evaluation_service/metrics/{metric_id}',
  } as const

  /** feature_matrix_plugin（Feature Matrix Plugin）：plugin.json 声明 4 端点 */
  export const FEATURE_MATRIX_PLUGIN_ENDPOINTS = {
    'config': '/ext/feature_matrix_plugin/config',
    'config_update': '/ext/feature_matrix_plugin/config',
    'wc_demo': '/ext/feature_matrix_plugin/wc_demo',
    'webview': '/ext/feature_matrix_plugin/webview',
  } as const

  /** hindsight_memory_service（Hindsight Memory Service）：plugin.json 声明 23 端点 */
  export const HINDSIGHT_MEMORY_SERVICE_ENDPOINTS = {
    'kb_list': '/ext/hindsight_memory_service/knowledge-base',
    'kb_categories_list': '/ext/hindsight_memory_service/knowledge-base/categories',
    'kb_category_create': '/ext/hindsight_memory_service/knowledge-base/categories',
    'kb_category_delete': '/ext/hindsight_memory_service/knowledge-base/categories/{name}',
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

  /** metrics_admin（Metrics Admin HTTP Face）：plugin.json 声明 3 端点 */
  export const METRICS_ADMIN_ENDPOINTS = {
    'prometheus': '/ext/metrics_admin/prometheus',
    'query': '/ext/metrics_admin/query',
    'series': '/ext/metrics_admin/series',
  } as const

  /** monitoring（Monitoring Service）：plugin.json 声明 10 端点 */
  export const MONITORING_ENDPOINTS = {
    'mon_cache_stats': '/ext/monitoring/cache-stats',
    'mon_payload_diag_page': '/ext/monitoring/page/payload-diag',
    'mon_tool_calls_page': '/ext/monitoring/page/tool-calls',
    'mon_payload_diag_list': '/ext/monitoring/payload-diag',
    'mon_payload_diag_get': '/ext/monitoring/payload-diag/file',
    'mon_system_metrics': '/ext/monitoring/system/metrics',
    'mon_tasks': '/ext/monitoring/tasks',
    'mon_task_statistics': '/ext/monitoring/tasks/statistics',
    'mon_token_usage': '/ext/monitoring/token-usage',
    'mon_tool_calls': '/ext/monitoring/tool-calls',
  } as const

  /** pipeline_godot_context（Godot Context）：plugin.json 声明 4 端点 */
  export const PIPELINE_GODOT_CONTEXT_ENDPOINTS = {
    'selection_preview': '/ext/pipeline_godot_context/preview',
    'selection_push': '/ext/pipeline_godot_context/selection',
    'selection_snapshot': '/ext/pipeline_godot_context/selection',
    'selection_subscribe': '/ext/pipeline_godot_context/subscribe',
  } as const

  /** pipeline_security_check（Security Check）：plugin.json 声明 2 端点 */
  export const PIPELINE_SECURITY_CHECK_ENDPOINTS = {
    'permission_mode_get': '/ext/pipeline_security_check/permission_mode',
    'permission_mode_switch': '/ext/pipeline_security_check/permission_mode',
  } as const

  /** task_form（Task Form Service）：plugin.json 声明 3 端点 */
  export const TASK_FORM_ENDPOINTS = {
    'task_form_get': '/ext/task_form/form',
    'task_form_agents': '/ext/task_form/options/agents',
    'task_form_containers': '/ext/task_form/options/containers',
  } as const

  /** task_service（Task Service）：plugin.json 声明 28 端点 */
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
    'tasks_containers': '/ext/task_service/tasks/containers',
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

  /** user_admin（User Admin HTTP Face）：plugin.json 声明 4 端点 */
  export const USER_ADMIN_ENDPOINTS = {
    'users': '/ext/user_admin/users',
    'user_delete': '/ext/user_admin/users/{user_id}',
    'user_role_update': '/ext/user_admin/users/{user_id}/role',
    'user_tenant_update': '/ext/user_admin/users/{user_id}/tenant',
  } as const

  /** widget_demo（Widget Demo（前端特性演示））：plugin.json 声明 8 端点 */
  export const WIDGET_DEMO_ENDPOINTS = {
    'demo_submit': '/ext/widget_demo/actions/submit',
    'demo_toggle': '/ext/widget_demo/actions/toggle',
    'demo_config_get': '/ext/widget_demo/config',
    'demo_config_put': '/ext/widget_demo/config',
    'demo_options_models': '/ext/widget_demo/options/models',
    'demo_options_regions': '/ext/widget_demo/options/regions',
    'demo_schema': '/ext/widget_demo/schema',
    'demo_state': '/ext/widget_demo/state',
  } as const

  /** workspace_service（Workspace Service）：plugin.json 声明 11 端点 */
  export const WORKSPACE_SERVICE_ENDPOINTS = {
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
