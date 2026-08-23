/**
 * queryKey 单一真值源
 *
 * 所有 useQuery / fetchQuery / invalidateQueries 的 key 必须从这里取，
 * 禁止在调用点手写数组字面量（防止同数据异 key 导致缓存失效与重复请求）。
 * 带参 key 用工厂函数（参数进 key，参数变 = 缓存条目变）。
 */

export const queryKeys = {
  /** 会话列表（GET /sessions?session_type=main_pipeline） */
  sessions: ['sessions'] as const,
  /** agent 列表（GET /ext/agent_manager/agents） */
  agents: ['agents'] as const,
  /** 聚合 schema（agents/pipelines/tools/routes/plugin_configs/plugin_contributes） */
  schema: ['schema'] as const,
  /** 插件列表（状态+能力面组合） */
  plugins: ['plugins'] as const,
  /** 单条管道配置（按配置名分条缓存） */
  pipelineConfig: (name: string) => ['pipeline-config', name] as const,
  /** LLM 服务配置 */
  llmConfig: ['llm-config'] as const,
  /** 调试中心：任务列表 */
  debugTasks: ['debug', 'tasks'] as const,
  /** 调试中心：会话列表 */
  debugSessions: ['debug', 'sessions'] as const,
  /** 调试中心：执行记录（按会话分条） */
  executionRecords: (sessionId?: string) => ['debug', 'execution-records', sessionId ?? 'all'] as const,
  /** 调试中心：评估指标 */
  evaluationMetrics: ['debug', 'evaluation-metrics'] as const,
  /** 调试中心：用户列表 */
  debugUsers: ['debug', 'users'] as const,
  /** 调试中心：LLM 请求诊断列表（按页分条） */
  llmPayloadDiag: (page: number) => ['debug', 'llm-payload', page] as const,
  /** 管理端：数据库表列表 */
  dbTables: ['debug', 'db-tables'] as const,
  /** 记忆：episodes 分页（页码进 key） */
  memoryEpisodes: (page: number) => ['memory', 'episodes', page] as const,
  /** 记忆：统计 */
  memoryStats: ['memory', 'stats'] as const,
  /** 知识库：文件列表 */
  kbFiles: ['knowledge-base', 'files'] as const,
  /** 管理页：用户列表 */
  adminUsers: ['admin', 'users'] as const,
  /** 管理页：用户统计 */
  adminUserStats: ['admin', 'user-stats'] as const,
  /** 长期任务列表（GET /ext/task_service/tasks，客户端过滤 long-term 标签） */
  longTermTasks: ['long-term-tasks'] as const,
  /** 管道管理面板全量任务列表（GET /ext/task_service/tasks 全量不过滤，任务节点/任务管道判定权威源） */
  pipelineAllTasks: ['pipeline-all-tasks'] as const,
  /** 管道 runs 快照（GET /api/v1/pipelines/runs） */
  pipelineRuns: ['pipeline-runs'] as const,
  /** 管道 states 快照（GET /api/v1/pipelines/state） */
  pipelineStates: ['pipeline-states'] as const,
} as const
