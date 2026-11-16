/**
 * 生成物：插件 contributes.pages 声明的投影 —— 勿手改！
 *
 * 唯一真值源 = 各插件 plugin.json 的 contributes.pages 声明
 * （审查总册 E6：壳层导航软绑定声明面化）。
 * 壳层导航只引用本文件的声明键（PLUGIN_DECLARED_PAGES[pluginId][pageId]），
 * 引用未声明页 = tsc 构建期红；运行时解析走 contributionRegistry 活注册表
 * （schema 刷新即随动），本生成物不承载运行时数据。
 * 改动插件页面声明后执行再生成：
 *     python scripts/gen_frontend_plugin_pages.py
 * 漂移检查（断链场景构建期报错）：
 *     python scripts/check_frontend_plugin_pages_sync.py
 */
/* eslint-disable */

/**
 * 壳层可引用的声明页键（嵌套 pluginId → pageId，与注册表 pluginId+id
 * 去重键同构）。path 为声明直连路由（文档面；运行时以注册表活值为准）。
 */
export const PLUGIN_DECLARED_PAGES = {
  acp_quota_pool: {
    acp_gateway_console: { pluginId: 'acp_quota_pool', pageId: 'acp_gateway_console', path: '/p/acp_gateway_console' },
  },
  agent_manager: {
    agent_manager_agents: { pluginId: 'agent_manager', pageId: 'agent_manager_agents', path: '/agents' },
  },
  browser_tool: {
    browser_live: { pluginId: 'browser_tool', pageId: 'browser_live', path: '/p/browser_live' },
  },
  debug_center: {
    debug_center_hub: { pluginId: 'debug_center', pageId: 'debug_center_hub' },
    db_admin: { pluginId: 'debug_center', pageId: 'db_admin' },
    execution_records: { pluginId: 'debug_center', pageId: 'execution_records' },
    pipeline_state: { pluginId: 'debug_center', pageId: 'pipeline_state' },
    sessions: { pluginId: 'debug_center', pageId: 'sessions' },
    tasks: { pluginId: 'debug_center', pageId: 'tasks' },
    users: { pluginId: 'debug_center', pageId: 'users' },
    evaluation: { pluginId: 'debug_center', pageId: 'evaluation' },
    llm_payload: { pluginId: 'debug_center', pageId: 'llm_payload' },
    contract_status: { pluginId: 'debug_center', pageId: 'contract_status' },
  },
  dsh_adapter: {
    dsh_plugin_manager: { pluginId: 'dsh_adapter', pageId: 'dsh_plugin_manager', path: '/p/dsh_plugin_manager' },
  },
  hindsight_memory_service: {
    memory: { pluginId: 'hindsight_memory_service', pageId: 'memory' },
  },
  isolation_service: {
    isolation_settings: { pluginId: 'isolation_service', pageId: 'isolation_settings' },
  },
  llm_service: {
    llm_settings: { pluginId: 'llm_service', pageId: 'llm_settings' },
  },
  mode_coding: {
    coding_delivery: { pluginId: 'mode_coding', pageId: 'coding_delivery', path: '/p/coding_delivery' },
  },
  mode_evolution: {
    evolution_panel: { pluginId: 'mode_evolution', pageId: 'evolution_panel', path: '/p/evolution_panel' },
  },
  mode_godot: {
    godot_dev: { pluginId: 'mode_godot', pageId: 'godot_dev', path: '/p/godot_dev' },
  },
  mode_personal: {
    pim_today: { pluginId: 'mode_personal', pageId: 'pim_today', path: '/p/agentos_pim_today' },
  },
  mode_research: {
    research_desk: { pluginId: 'mode_research', pageId: 'research_desk', path: '/p/research_desk' },
  },
  mode_roleplay: {
    roleplay_studio: { pluginId: 'mode_roleplay', pageId: 'roleplay_studio', path: '/p/roleplay_studio' },
  },
  mode_writing: {
    writing_workshop: { pluginId: 'mode_writing', pageId: 'writing_workshop', path: '/p/writing_workshop' },
  },
  monitoring: {
    monitoring: { pluginId: 'monitoring', pageId: 'monitoring', path: '/monitoring' },
    tool_call_records: { pluginId: 'monitoring', pageId: 'tool_call_records', path: '/p/tool_call_records' },
  },
  onboarding_service: {
    get_started: { pluginId: 'onboarding_service', pageId: 'get_started', path: '/p/get_started' },
    onboarding_guide: { pluginId: 'onboarding_service', pageId: 'onboarding_guide' },
  },
  pipeline_security_check: {
    'zone-policy-settings': { pluginId: 'pipeline_security_check', pageId: 'zone-policy-settings' },
  },
  pipeline_todo_track: {
    todo_badge: { pluginId: 'pipeline_todo_track', pageId: 'todo_badge' },
    todo_plan: { pluginId: 'pipeline_todo_track', pageId: 'todo_plan', path: '/p/todo_plan' },
  },
  snippet_box: {
    snippet_box: { pluginId: 'snippet_box', pageId: 'snippet_box', path: '/p/snippet_box' },
  },
  trigger_setup_tool: {
    triggers: { pluginId: 'trigger_setup_tool', pageId: 'triggers', path: '/triggers' },
  },
  user_admin: {
    admin: { pluginId: 'user_admin', pageId: 'admin', path: '/admin' },
  },
} as const

/**
 * 已声明页面引用（判别联合）：pluginId+pageId 须均为声明成员，否则 tsc 红。
 * 壳层告警动作/菜单项等引用面统一收窄到本类型。
 */
export type DeclaredPluginPageRef = {
  [K in keyof typeof PLUGIN_DECLARED_PAGES & string]: {
    [P in keyof (typeof PLUGIN_DECLARED_PAGES)[K] & string]: { pluginId: K; pageId: P }
  }[keyof (typeof PLUGIN_DECLARED_PAGES)[K] & string]
}[keyof typeof PLUGIN_DECLARED_PAGES & string]

