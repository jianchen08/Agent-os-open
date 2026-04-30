/**
 * UI Schema 类型定义
 *
 * 定义后端模块 UI Schema 的完整类型系统
 * Schema 分为四部分：identity、actions、rendering、clients
 */

/** 模块身份信息 */
export interface ModuleIdentity {
  /** 模块唯一标识 */
  id: string
  /** 模块名称 */
  name: string
  /** 模块版本 */
  version: string
  /** 模块分类 */
  category: 'builtin' | 'extension' | 'custom'
  /** 模块描述 */
  description?: string
  /** 模块图标 */
  icon?: string
  /** 模块作者 */
  author?: string
  /** 模块标签 */
  tags?: string[]
}

/** 模块操作定义 */
export interface ModuleAction {
  /** 操作 ID */
  id: string
  /** 操作名称 */
  name: string
  /** 操作类型 */
  type: 'command' | 'query' | 'event' | 'stream'
  /** 操作描述 */
  description?: string
  /** 输入参数 Schema */
  inputSchema?: Record<string, unknown>
  /** 输出参数 Schema */
  outputSchema?: Record<string, unknown>
  /** 是否需要确认 */
  requiresConfirmation?: boolean
  /** 是否为危险操作 */
  isDangerous?: boolean
}

/** 聊天交互模板类型 */
export type ChatInteractionType =
  | 'form'
  | 'chart'
  | 'gallery'
  | 'table'
  | 'progress'
  | 'code_block'
  | 'status_card'
  | 'decision'

/** 聊天交互组件配置 */
export interface ChatInteractionConfig {
  /** 交互类型 */
  type: ChatInteractionType
  /** 组件配置 */
  props?: Record<string, unknown>
  /** 数据源 */
  dataSource?: string
  /** 自动刷新间隔（毫秒） */
  refreshInterval?: number
}

/** 渲染空间类型 */
export type RenderingSpaceType = 'chat' | 'workspace' | 'floating' | 'dock' | 'fullscreen'

/** 渲染空间配置 */
export interface RenderingSpaceConfig {
  /** 渲染空间类型 */
  space: RenderingSpaceType
  /** 组件类型 */
  widget: string
  /** 组件属性 */
  props?: Record<string, unknown>
  /** 数据源 */
  dataSource?: string
  /** 布局配置 */
  layout?: {
    width?: number | string
    height?: number | string
    minWidth?: number
    minHeight?: number
    resizable?: boolean
    draggable?: boolean
    position?: 'auto' | 'center' | 'top-left' | 'top-right' | 'bottom-left' | 'bottom-right'
  }
  /** 自动弹出条件 */
  autoOpen?: {
    /** 触发事件 */
    event?: string
    /** 延迟（毫秒） */
    delay?: number
  }
}

/** 渲染配置 */
export interface ModuleRendering {
  /** 聊天交互模板列表 */
  chat: ChatInteractionConfig[]
  /** 渲染空间列表 */
  spaces: RenderingSpaceConfig[]
  /** Dock 图标配置 */
  dock?: {
    icon?: string
    label?: string
    /** 状态指示灯 */
    indicator?: 'none' | 'dot' | 'badge'
    indicatorColor?: string
  }
  /** 全屏触发条件 */
  fullscreen?: {
    /** 触发事件 */
    triggerEvent?: string
    /** 自动进入全屏 */
    autoEnter?: boolean
  }
}

/** 客户端能力要求 */
export interface ClientCapabilities {
  /** 要求的渲染空间 */
  requiredSpaces: RenderingSpaceType[]
  /** 要求的交互组件 */
  requiredWidgets: string[]
  /** 最低客户端版本 */
  minClientVersion?: string
  /** 降级方案 */
  fallback?: {
    /** 降级到的交互组件 */
    widget: string
    /** 降级到的渲染空间 */
    space: RenderingSpaceType
  }
}

/** 完整的模块 UI Schema */
export interface ModuleUISchema {
  /** 模块身份 */
  identity: ModuleIdentity
  /** 模块操作 */
  actions: ModuleAction[]
  /** 渲染配置 */
  rendering: ModuleRendering
  /** 客户端能力要求 */
  clients: ClientCapabilities
}

/** Schema 解析结果 */
export interface ParsedSchema {
  /** 原始 Schema */
  raw: ModuleUISchema
  /** 解析后的身份信息 */
  identity: ModuleIdentity
  /** 解析后的操作列表 */
  actions: ModuleAction[]
  /** 解析后的渲染配置 */
  rendering: ModuleRendering
  /** 解析后的客户端要求 */
  clients: ClientCapabilities
  /** 解析时间戳 */
  parsedAt: number
  /** Schema 版本哈希 */
  versionHash: string
}

/** 数据源引用格式：module://collection */
export interface DataSourceRef {
  /** 模块 ID */
  moduleId: string
  /** 数据集合名称 */
  collection: string
  /** 查询参数 */
  query?: Record<string, unknown>
  /** 过滤条件 */
  filter?: Record<string, unknown>
  /** 排序 */
  sort?: string
  /** 分页 */
  pagination?: {
    page: number
    pageSize: number
  }
}

/** 数据源解析结果 */
export interface ResolvedDataSource {
  /** API 端点 */
  endpoint: string
  /** 请求方法 */
  method: 'GET' | 'POST'
  /** 请求参数 */
  params: Record<string, unknown>
  /** 是否支持轮询 */
  supportsPolling: boolean
  /** 轮询间隔 */
  pollInterval?: number
}

/** 模块注册信息 */
export interface ModuleRegistration {
  /** 模块 Schema */
  schema: ModuleUISchema
  /** 注册时间 */
  registeredAt: number
  /** 是否启用 */
  enabled: boolean
  /** 来源 */
  source: 'api' | 'local' | 'push'
}
