/**
 * 管道运行快照类型（统一管道管理）
 *
 * 数据源：`GET /api/v1/pipelines/runs`（内核 runs × message_slots ×
 * pipeline_sessions × pipeline_run_summaries 四表联结）。前端以 pipeline_id
 * 为主键维护注册表，同一管道多条 run 取最新。
 */

/** 管道运行状态（对齐内核 RunStatus，lowercase） */
export type PipelineStatus = 'running' | 'suspended' | 'completed' | 'failed'

/** 管道运行快照条目 */
export interface PipelineRunInfo {
  /** 运行实例 ID（UUID） */
  run_id: string
  /** 所属管道 ID（消息层主键；旧引擎占位 run 已被查询层过滤） */
  pipeline_id?: string
  /** 归属会话（thread）ID */
  thread_id?: string
  /** 运行状态 */
  status: PipelineStatus
  /** 开始时间（ISO8601） */
  started_at: string
  /** 结束时间（ISO8601，None = 未结束） */
  ended_at?: string
  /** token 用量明细（input/output/... 键值；sidecar 未汇总时为 null） */
  total_tokens?: Record<string, number> | null
  /** 总耗时秒（sidecar 未汇总时为 null） */
  total_seconds?: number | null
}

/** 管道管理条目（运行快照 + 前端派生的类型/名称/实时 token） */
export interface PipelineViewEntry {
  /** 主键：pipeline_id（缺失时回退 run_id） */
  key: string
  /** 管道 ID */
  pipelineId?: string
  /** 运行 ID */
  runId: string
  /** 归属会话 ID */
  threadId?: string
  /** 运行状态 */
  status: PipelineStatus
  /** 开始时间（ISO8601） */
  startedAt: string
  /** 结束时间（ISO8601，未结束为 undefined） */
  endedAt?: string
  /** 类型：任务管道 / 会话管道 */
  kind: 'task' | 'session'
  /** 展示名称（任务标题 / 会话标题 / 短 ID 兜底） */
  name: string
  /** 执行者 Agent 名称（可空） */
  agentName?: string
  /** 关联任务 ID（kind=task 时有值） */
  taskId?: string
  /** 归属会话标题（threadId 命中会话列表时有值；无则视为无归属孤儿管道） */
  sessionTitle?: string
  /** 工作区坐标（R3：state 真值 ws_meta.path/workspace 优先，任务 metadata
   *  回退；任意 kind 有坐标即可"打开工作空间"，主会话无坐标则无按钮） */
  workspacePath?: string
  /** 任务进度（0-100，kind=task 且任务带进度时有值） */
  progress?: number
  /** 汇总 token 用量（内核 summaries；实时以 usage 覆盖） */
  totalTokens?: Record<string, number> | null
  /** 实时 token 用量（cost_update 事件；缺失时回退 totalTokens） */
  liveUsage?: {
    promptTokens: number
    completionTokens: number
    totalTokens: number
  }
  /** 当前循环体阶段（内核 state.current_phase：init/main/exit…，多循环体真值） */
  currentPhase?: string
  /** 消息条数（state.messages 规模，迭代轮次的粗粒度指标） */
  messageCount?: number
  /** 任务原始状态（task.status——evaluating/planning 等细态不被 4 态映射吞掉） */
  taskStatus?: string
  /** 管道 state 真值状态（state['task.status'] ?? state.status） */
  stateStatus?: string
  /** 管道 state 是否已结束（state.ended） */
  stateEnded?: boolean
  /** 管道 state 原始错误（state.raw_error） */
  rawError?: string
}
