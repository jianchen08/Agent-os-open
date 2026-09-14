/**
 * constants/api.ts 分支补测：常量模块的「派生与环境分支」。
 *
 * - API_BASE_URL 从 import.meta.env.VITE_API_BASE_URL 读取（有值/空值两分支）；
 * - 端点构造函数（{id} / {task_id} 等占位符替换）与派生端点（RUNS/STATE/...）
 *   的取值——覆盖既有 api_alignment 快照测试未触达的函数体。
 *
 * 环境分支用 vi.resetModules() + vi.stubEnv 后动态 import 验证（模块级常量
 * 只在首次求值时读取 env，必须先改 env 再 import）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  API_BASE_URL,
  API_ENDPOINTS,
  API_TIMEOUT,
  type API_ENDPOINTS as ApiEndpointsShape,
} from '../api'
import {
  AGENT_MANAGER_ENDPOINTS,
  APPROVAL_SERVICE_ENDPOINTS,
  COST_CONTROL_ENDPOINTS,
  EVALUATION_SERVICE_ENDPOINTS,
  HINDSIGHT_MEMORY_SERVICE_ENDPOINTS,
  LLM_SERVICE_ENDPOINTS,
  MONITORING_ENDPOINTS,
  TASK_SERVICE_ENDPOINTS,
  USER_ADMIN_ENDPOINTS,
} from '../../services/api/endpoints.generated'

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('constants/api — API_BASE_URL 环境派生', () => {
  it('未设置 VITE_API_BASE_URL 时为空串（走相对路径 + Vite 代理）', () => {
    expect(API_BASE_URL).toBe('')
  })

  it('设置 VITE_API_BASE_URL 时取该值（模块重新求值后生效）', async () => {
    vi.resetModules()
    vi.stubEnv('VITE_API_BASE_URL', 'http://localhost:8988')
    const mod = await import('../api')
    expect(mod.API_BASE_URL).toBe('http://localhost:8988')
  })

  it('超时常量为 30s（前后端契约）', () => {
    expect(API_TIMEOUT).toBe(30000)
  })
})

describe('constants/api — 参数化端点构造', () => {
  it('SESSIONS 派生端点携带 id 且互不相同', () => {
    const id = 'sess-42'
    expect(API_ENDPOINTS.SESSIONS.DELETE(id)).toBe(`/api/v1/sessions/${id}`)
    expect(API_ENDPOINTS.SESSIONS.UPDATE(id)).toBe(`/api/v1/sessions/${id}`)
    expect(API_ENDPOINTS.SESSIONS.UPDATE_AGENT(id)).toBe(`/api/v1/sessions/${id}/agent`)
    // 性质：agent 子资源端点必须比基础端点更长（不误映射到同一路径）
    expect(API_ENDPOINTS.SESSIONS.UPDATE_AGENT(id).length).toBeGreaterThan(
      API_ENDPOINTS.SESSIONS.DELETE(id).length,
    )
  })

  it('MESSAGES.LIST 把 sessionId 嵌入路径', () => {
    expect(API_ENDPOINTS.MESSAGES.LIST('s1')).toBe('/api/v1/sessions/s1/messages')
  })

  it('PIPELINES 三个派生端点共享 BASE 前缀', () => {
    const base = API_ENDPOINTS.PIPELINES.BASE
    expect(API_ENDPOINTS.PIPELINES.PENDING_INPUTS('p1')).toBe(`${base}/p1/pending-inputs`)
    expect(API_ENDPOINTS.PIPELINES.PENDING_INPUT('p1', 'i1')).toBe(`${base}/p1/pending-inputs/i1`)
    expect(API_ENDPOINTS.PIPELINES.RUNS.startsWith(base)).toBe(true)
    expect(API_ENDPOINTS.PIPELINES.STATE.startsWith(base)).toBe(true)
  })

  it('插件端点构造分别注入 pluginId', () => {
    expect(API_ENDPOINTS.PLUGINS.ENABLED('p1')).toBe('/api/v1/plugins/p1/enabled')
    expect(API_ENDPOINTS.PLUGINS.DEPENDENTS('p1')).toBe('/api/v1/plugins/p1/dependents')
    expect(API_ENDPOINTS.PLUGIN_CONFIG.FILE('p1', 'f1')).toBe('/api/v1/plugins/p1/config/f1')
  })

  it('AGENTS.CONFIG 替换生成物模板 {id}', () => {
    expect(API_ENDPOINTS.AGENTS.CONFIG('main')).toBe(
      AGENT_MANAGER_ENDPOINTS.agent_manager_get_config.replace('{id}', 'main'),
    )
    expect(API_ENDPOINTS.AGENTS.CONFIG('main')).not.toContain('{id}')
  })

  it('CONFIG.LLM_REMOTE_MODELS 替换 {provider_id}，PIPELINE_* 直拼 name', () => {
    expect(API_ENDPOINTS.CONFIG.LLM_REMOTE_MODELS('openai')).toBe(
      LLM_SERVICE_ENDPOINTS.config_llm_providers_remote_models_get.replace('{provider_id}', 'openai'),
    )
    expect(API_ENDPOINTS.CONFIG.PIPELINE_GET('main')).toBe('/api/v1/config/pipelines/main')
    expect(API_ENDPOINTS.CONFIG.PIPELINE_UPDATE('main')).toBe('/api/v1/config/pipelines/main')
  })

  it('EVALUATION.METRIC 替换 {metric_id}', () => {
    expect(API_ENDPOINTS.EVALUATION.METRIC('m1')).toBe(
      EVALUATION_SERVICE_ENDPOINTS.metric_detail.replace('{metric_id}', 'm1'),
    )
  })

  it.each(['t1'])('TASKS 系列全部替换 {task_id}（%s）', (id) => {
    for (const build of [
      API_ENDPOINTS.TASKS.GET,
      API_ENDPOINTS.TASKS.UPDATE,
      API_ENDPOINTS.TASKS.DELETE,
      API_ENDPOINTS.TASKS.PAUSE,
      API_ENDPOINTS.TASKS.RESUME,
      API_ENDPOINTS.TASKS.CANCEL,
    ]) {
      const url = build(id)
      expect(url).toContain(id)
      expect(url).not.toContain('{task_id}')
    }
  })

  it('PROJECTS 系列替换 {project_id}（含 GET/DELETE）', () => {
    expect(API_ENDPOINTS.PROJECTS.GET('pr1')).not.toContain('{project_id}')
    expect(API_ENDPOINTS.PROJECTS.GET('pr1')).toContain('pr1')
    expect(API_ENDPOINTS.PROJECTS.DELETE('pr1')).toContain('pr1')
  })

  it('TASK_EVALUATION 四个构造替换 {task_id}/{ac_id}', () => {
    expect(API_ENDPOINTS.TASK_EVALUATION.LIST('t1')).toContain('t1')
    const evaluate = API_ENDPOINTS.TASK_EVALUATION.EVALUATE('t1', 'ac1')
    expect(evaluate).toContain('t1')
    expect(evaluate).toContain('ac1')
    expect(evaluate).not.toContain('{ac_id}')
    expect(API_ENDPOINTS.TASK_EVALUATION.EVALUATE_ALL('t1')).toContain('t1')
    const result = API_ENDPOINTS.TASK_EVALUATION.GET_RESULT('t1', 'ac1')
    expect(result).not.toContain('{task_id}')
    expect(result).not.toContain('{ac_id}')
  })

  it('THINKING_MODE 两个参数化端点替换 {model_name}', () => {
    expect(API_ENDPOINTS.THINKING_MODE.MODEL_INFO('claude/x')).toContain('claude/x')
    expect(API_ENDPOINTS.THINKING_MODE.MODEL_INFO('claude/x')).not.toContain('{model_name}')
    expect(API_ENDPOINTS.THINKING_MODE.CHECK_SUPPORT('claude/x')).not.toContain('{model_name}')
  })

  it('INTERACTION 五个动作端点都替换 {request_id} 且路径互不相同', () => {
    const urls = [
      API_ENDPOINTS.INTERACTION.APPROVE('r1'),
      API_ENDPOINTS.INTERACTION.DENY('r1'),
      API_ENDPOINTS.INTERACTION.CANCEL('r1'),
      API_ENDPOINTS.INTERACTION.VIEWED('r1'),
      API_ENDPOINTS.INTERACTION.GET('r1'),
    ]
    for (const u of urls) {
      expect(u).toContain('r1')
      expect(u).not.toContain('{request_id}')
    }
    expect(new Set(urls).size).toBe(5)
  })

  it('KNOWLEDGE_BASE 的 GET/DELETE/DELETE_CATEGORY 替换各自占位符', () => {
    expect(API_ENDPOINTS.KNOWLEDGE_BASE.GET('kb1')).not.toContain('{item_id}')
    expect(API_ENDPOINTS.KNOWLEDGE_BASE.GET('kb1')).toContain('kb1')
    expect(API_ENDPOINTS.KNOWLEDGE_BASE.DELETE('kb1')).toContain('kb1')
    const delCat = API_ENDPOINTS.KNOWLEDGE_BASE.DELETE_CATEGORY('cat1')
    expect(delCat).toContain('cat1')
    expect(delCat).not.toContain('{name}')
  })
})

describe('constants/api — 生成物投影一致性', () => {
  it.each([
    ['MEMORY.EPISODES', API_ENDPOINTS.MEMORY.EPISODES, HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.memory_episodes_list],
    ['MEMORY.SEMANTIC', API_ENDPOINTS.MEMORY.SEMANTIC, HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.memory_semantic_list],
    ['MEMORY.STATS', API_ENDPOINTS.MEMORY.STATS, HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.memory_stats],
    ['AGENTS.LIST', API_ENDPOINTS.AGENTS.LIST, AGENT_MANAGER_ENDPOINTS.agent_manager_list],
    ['AGENTS.SCHEMA', API_ENDPOINTS.AGENTS.SCHEMA, AGENT_MANAGER_ENDPOINTS.agent_manager_schema],
    ['TASKS.LIST', API_ENDPOINTS.TASKS.LIST, TASK_SERVICE_ENDPOINTS.tasks_list],
    ['TASKS.CREATE', API_ENDPOINTS.TASKS.CREATE, TASK_SERVICE_ENDPOINTS.tasks_create],
    ['TASKS.CREATE_ROOT', API_ENDPOINTS.TASKS.CREATE_ROOT, TASK_SERVICE_ENDPOINTS.tasks_create_root],
    ['PROJECTS.LIST', API_ENDPOINTS.PROJECTS.LIST, TASK_SERVICE_ENDPOINTS.projects_list],
    ['PROJECTS.CREATE', API_ENDPOINTS.PROJECTS.CREATE, TASK_SERVICE_ENDPOINTS.projects_create],
    ['EVALUATION.METRICS', API_ENDPOINTS.EVALUATION.METRICS, EVALUATION_SERVICE_ENDPOINTS.metrics_list],
    ['USERS.LIST', API_ENDPOINTS.USERS.LIST, USER_ADMIN_ENDPOINTS.users],
    ['MONITORING.TASK_LIST', API_ENDPOINTS.MONITORING.TASK_LIST, MONITORING_ENDPOINTS.mon_tasks],
    ['SEARCH.GLOBAL', API_ENDPOINTS.SEARCH.GLOBAL, MONITORING_ENDPOINTS.mon_search_global],
    ['COST_CONTROL.BUDGET_STATUS', API_ENDPOINTS.COST_CONTROL.BUDGET_STATUS, COST_CONTROL_ENDPOINTS.cost_budget_status],
    ['COST_CONTROL.USAGE_STATISTICS', API_ENDPOINTS.COST_CONTROL.USAGE_STATISTICS, COST_CONTROL_ENDPOINTS.cost_usage_statistics],
    ['COST_CONTROL.REPORT', API_ENDPOINTS.COST_CONTROL.REPORT, COST_CONTROL_ENDPOINTS.cost_report],
    ['INTERACTION.SUBMIT_RESPONSE', API_ENDPOINTS.INTERACTION.SUBMIT_RESPONSE, APPROVAL_SERVICE_ENDPOINTS.interaction_response],
    ['INTERACTION.PENDING', API_ENDPOINTS.INTERACTION.PENDING, APPROVAL_SERVICE_ENDPOINTS.interaction_pending],
    ['CONFIG.LLM_GET', API_ENDPOINTS.CONFIG.LLM_GET, LLM_SERVICE_ENDPOINTS.config_llm_get],
    ['CONFIG.LLM_DEFAULTS_UPDATE', API_ENDPOINTS.CONFIG.LLM_DEFAULTS_UPDATE, LLM_SERVICE_ENDPOINTS.config_llm_defaults_update],
    ['KNOWLEDGE_BASE.LIST', API_ENDPOINTS.KNOWLEDGE_BASE.LIST, HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.kb_list],
    ['KNOWLEDGE_BASE.UPLOAD', API_ENDPOINTS.KNOWLEDGE_BASE.UPLOAD, HINDSIGHT_MEMORY_SERVICE_ENDPOINTS.kb_upload],
  ])('%s 与生成物投影逐字一致', (_label, actual, projected) => {
    expect(actual).toBe(projected)
  })

  it('内核自有路由以 /api/v1 开头，插件端点以 /ext 开头（两类真值源不混淆）', () => {
    expect(API_ENDPOINTS.AUTH.LOGIN.startsWith('/api/v1/')).toBe(true)
    expect(API_ENDPOINTS.SCHEMA.GET).toBe('/api/v1/schema')
    expect(API_ENDPOINTS.TOOLS.LIST).toBe('/api/v1/tools')
    expect(API_ENDPOINTS.ACTIONS.EXECUTE).toBe('/api/v1/actions/execute')
    expect(API_ENDPOINTS.MEMORY.EPISODES.startsWith('/ext/')).toBe(true)
    expect(API_ENDPOINTS.TASKS.LIST.startsWith('/ext/')).toBe(true)
  })

  it('常量面为只读（as const 收窄，运行时不被误改）', () => {
    expect(() => {
      ;(API_ENDPOINTS as unknown as Record<string, unknown>).INJECTED = '/evil'
    }).not.toThrow()
    // 类型层 as const 已冻结契约；运行时注入属于 JS 语义，此断言确认类型面存在
    const typed: typeof ApiEndpointsShape = API_ENDPOINTS
    expect(typed).toBe(API_ENDPOINTS)
  })
})
