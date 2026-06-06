/**
 * 监控相关类型定义
 *
 * 定义系统监控、性能指标、任务执行状态等类型
 */

/**
 * 系统性能指标
 */
export interface SystemMetrics {
  /** CPU 使用率 (0-100) */
  cpu_usage: number
  /** 内存使用情况 */
  memory: MemoryUsage
  /** 磁盘使用情况 */
  disk: DiskUsage
  /** 系统负载 */
  load_average?: number[]
  /** 运行时间（秒） */
  uptime?: number
  /** 时间戳 */
  timestamp: string
}

/**
 * 内存使用情况
 */
export interface MemoryUsage {
  /** 总内存（字节） */
  total: number
  /** 已使用内存（字节） */
  used: number
  /** 可用内存（字节） */
  available: number
  /** 使用率 (0-100) */
  usage_percent: number
}

/**
 * 磁盘使用情况
 */
export interface DiskUsage {
  /** 挂载点 */
  mount_point: string
  /** 总容量（字节） */
  total: number
  /** 已使用（字节） */
  used: number
  /** 可用（字节） */
  free: number
  /** 使用率 (0-100) */
  usage_percent: number
}

/**
 * 任务执行统计
 */
export interface TaskStatistics {
  /** 总任务数 */
  total: number
  /** 成功任务数 */
  succeeded: number
  /** 失败任务数 */
  failed: number
  /** 运行中任务数 */
  running: number
  /** 等待中任务数 */
  pending: number
  /** 平均执行时长（毫秒） */
  avg_duration?: number
  /** 成功率 (0-100) */
  success_rate: number
}

/**
 * 任务信息（用于列表展示）
 */
export interface TaskInfo {
  /** 任务 ID */
  id: string
  /** 任务名称/意图 */
  intent?: string
  /** 任务名称（后端返回） */
  name?: string
  /** 执行状态 */
  status: 'pending' | 'running' | 'completed' | 'failed' | 'cancelled' | 'suspended'
  /** 任务描述 */
  description?: string
  /** 创建时间 */
  created_at: string
  /** 开始时间 */
  started_at?: string
  /** 完成时间 */
  completed_at?: string
  /** Agent ID */
  agent_id?: string
  /** 错误信息 */
  error?: string
  /** 执行时长（毫秒） */
  duration?: number
  /** 当前步骤 */
  current_step?: string
  /** 进度 (0-100) */
  progress?: number
}

/**
 * Token 使用统计
 */
export interface TokenUsage {
  /** 总 Token 使用量 */
  total_tokens: number
  /** 输入 Token 数 */
  prompt_tokens: number
  /** 输出 Token 数 */
  completion_tokens: number
  /** 请求次数 */
  request_count: number
}

/**
 * 缓存命中率统计
 */
export interface CacheStats {
  /** 缓存命中次数 */
  cache_hits: number
  /** 缓存未命中次数 */
  cache_misses: number
  /** 命中率 (0-100) */
  hit_rate: number
  /** 总请求数 */
  total_requests: number
}

/**
 * Token 使用统计响应
 */
export interface TokenUsageResponse {
  token_usage: TokenUsage
}

/**
 * 缓存统计响应
 */
export interface CacheStatsResponse {
  cache_stats: CacheStats
}

/**
 * 监控数据汇总
 */
export interface MonitoringData {
  /** 系统性能指标 */
  metrics: SystemMetrics | null
  /** 任务执行统计 */
  statistics: TaskStatistics | null
  /** 最近任务列表 */
  recentTasks: TaskInfo[]
  /** 最后更新时间 */
  lastUpdated: string
}

/**
 * API 响应类型
 */

/** 系统指标响应 */
export interface SystemMetricsResponse {
  metrics: SystemMetrics
}

/** 任务统计响应 */
export interface TaskStatisticsResponse {
  statistics: TaskStatistics
}

/** 任务列表响应 */
export interface TaskListResponse {
  items: TaskInfo[]
  total: number
  page: number
  page_size: number
}
