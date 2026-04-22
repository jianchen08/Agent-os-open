/**
 * 触发器相关类型定义
 *
 * 与后端 /api/v1/triggers/* 端点对齐
 */

/**
 * 触发器类型枚举
 */
export enum TriggerType {
  /** 时间触发器 */
  TIME = 'time',
  /** 事件触发器 */
  EVENT = 'event',
  /** 条件触发器 */
  CONDITION = 'condition',
}

/**
 * 触发器响应类型
 */
export interface Trigger {
  /** 触发器 ID */
  id: string
  /** 触发器名称 */
  name: string
  /** 触发器类型 */
  trigger_type: string
  /** 是否启用 */
  enabled: boolean
  /** 执行次数 */
  execution_count: number
  /** 最后执行时间 */
  last_execution?: string
  /** 最后执行结果 */
  last_result?: Record<string, any>
  /** 触发器配置 */
  config: TriggerConfig
}

/**
 * 触发器配置类型
 */
export interface TriggerConfig {
  /** 触发器描述 */
  description?: string
  /** 动作列表 */
  actions: TriggerAction[]
  /** 元数据 */
  metadata?: Record<string, any>
  /** 时间调度配置 */
  schedule?: ScheduleConfig
  /** 事件配置 */
  event?: EventConfig
  /** 条件配置 */
  condition?: ConditionConfig
}

/**
 * 触发器动作类型
 */
export interface TriggerAction {
  /** 动作类型 */
  type: string
  /** 动作参数 */
  params: Record<string, any>
}

/**
 * 时间调度配置
 */
export interface ScheduleConfig {
  /** Cron 表达式 */
  cron?: string
  /** 间隔时间（秒） */
  interval?: number
  /** 执行时间 */
  at?: string
}

/**
 * 事件配置
 */
export interface EventConfig {
  /** 事件类型 */
  event_type: string
  /** 事件过滤器 */
  filters?: Record<string, any>
}

/**
 * 条件配置
 */
export interface ConditionConfig {
  /** 条件表达式 */
  expression: string
  /** 检查间隔（秒） */
  check_interval?: number
}

/**
 * 触发器列表响应类型
 */
export interface TriggerListResponse {
  /** 总数 */
  total: number
  /** 触发器列表 */
  triggers: Trigger[]
}

/**
 * 触发器统计响应类型
 */
export interface TriggerStatsResponse {
  /** 总触发器数 */
  total_triggers: number
  /** 已启用的触发器数 */
  enabled_triggers: number
  /** 已禁用的触发器数 */
  disabled_triggers: number
  /** 按类型统计 */
  type_counts: Record<string, number>
  /** 触发器 ID 列表 */
  trigger_ids: string[]
}

/**
 * 创建触发器请求类型
 */
export interface TriggerCreateRequest {
  /** 触发器 ID */
  id: string
  /** 触发器名称 */
  name: string
  /** 触发器类型 */
  trigger_type: string
  /** 是否启用 */
  enabled?: boolean
  /** 触发器描述 */
  description?: string
  /** 动作列表 */
  actions?: TriggerAction[]
  /** 元数据 */
  metadata?: Record<string, any>
  /** 时间调度配置 */
  schedule?: ScheduleConfig
  /** 事件配置 */
  event?: EventConfig
  /** 条件配置 */
  condition?: ConditionConfig
}

/**
 * 更新触发器请求类型
 */
export interface TriggerUpdateRequest {
  /** 触发器名称 */
  name?: string
  /** 是否启用 */
  enabled?: boolean
  /** 触发器描述 */
  description?: string
  /** 动作列表 */
  actions?: TriggerAction[]
  /** 元数据 */
  metadata?: Record<string, any>
  /** 时间调度配置 */
  schedule?: ScheduleConfig
  /** 事件配置 */
  event?: EventConfig
  /** 条件配置 */
  condition?: ConditionConfig
}

/**
 * 手动触发请求类型
 */
export interface ManualTriggerRequest {
  /** 触发上下文 */
  context?: Record<string, any>
}

/**
 * 操作结果类型
 */
export interface TriggerOperationResult {
  /** 操作状态 */
  status: string
  /** 触发器 ID */
  id: string
}

/**
 * 手动触发结果类型
 */
export interface ManualTriggerResult {
  /** 操作状态 */
  status: string
  /** 触发器 ID */
  trigger_id: string
  /** 执行结果 */
  result?: Record<string, any>
}

/**
 * 列出触发器查询参数
 */
export interface ListTriggersParams {
  /** 只返回已启用的触发器 */
  enabled_only?: boolean
  /** 过滤触发器类型 */
  trigger_type?: string
}
