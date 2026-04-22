/**
 * 工具类型定义
 *
 * 与后端 src/tools/types.py 和 src/db/models.py 保持对齐
 */

/** 工具来源 */
export type ToolSource = 'builtin' | 'mcp' | 'custom' | 'code' | 'http'

/** 工具分类 */
export type ToolCategory =
  | 'file'
  | 'search'
  | 'web'
  | 'memory'
  | 'task'
  | 'system'
  | 'execution'
  | 'analysis'
  | 'evaluation'
  | 'agent'
  | 'monitoring'
  | 'other'

/** 工具状态 */
export type ToolStatus = 'active' | 'disabled' | 'deprecated'

/** 工具级别 */
export type ToolLevel = 'system' | 'user' | 'l1_only' | 'l1_l2_only' | 'all'

/** 工具使用示例 */
export interface ToolExample {
  /** 示例输入参数 */
  input: Record<string, unknown>
  /** 预期输出 */
  output?: unknown
  /** 示例说明 */
  description?: string
}

/** 工具定义（与后端 Tool 类对齐） */
export interface Tool {
  /** 数据库 ID */
  id?: string
  /** 工具唯一标识 */
  name: string
  /** 工具功能描述（简短） */
  description: string

  /** 适用场景列表 */
  when_to_use?: string[]
  /** 不适用场景列表 */
  when_not_to_use?: string[]
  /** 使用示例列表 */
  examples?: ToolExample[]
  /** 注意事项列表 */
  caveats?: string[]

  /** 输入参数 JSON Schema */
  input_schema?: Record<string, unknown>
  /** 输出 Schema */
  output_schema?: Record<string, unknown>

  /** 工具来源 */
  source: ToolSource
  /** 工具分类 */
  category?: ToolCategory
  /** 工具级别 */
  level?: ToolLevel
  /** 版本号 */
  version?: string
  /** 标签 */
  tags?: string[]

  /** 工具状态 */
  status: ToolStatus
  /** 是否需要审批 */
  requires_approval?: boolean

  /** 创建时间 */
  created_at?: string
  /** 更新时间 */
  updated_at?: string
}

/** 工具列表响应 */
export interface ToolListResponse {
  /** 工具列表 */
  items: Tool[]
  /** 总数量 */
  total: number
  /** 当前页码 */
  page: number
  /** 每页数量 */
  page_size: number
}

/** 工具详情（包含完整描述） */
export interface ToolDetail extends Tool {
  /** 完整描述（包含使用边界） */
  full_description?: string
  /** 使用统计 */
  usage_stats?: {
    success_count: number
    failure_count: number
    last_used_at?: string
  }
}

/** 工具表单数据 */
export interface ToolFormData {
  name: string
  description: string
  category?: ToolCategory
  when_to_use?: string[]
  when_not_to_use?: string[]
  caveats?: string[]
  input_schema?: Record<string, unknown>
}

/** 代码条目 */
export interface CodeEntry {
  path: string
  content: string
  language: string
}
