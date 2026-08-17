/**
 * UI Schema 类型定义
 *
 * 定义后端模块 UI Schema 的完整类型系统
 * Schema 分为四部分：identity、actions、rendering、clients
 *
 * 0.2 扩展：
 * - ui 字段（input_form / result_widget）—— SchemaDriver 新解析能力
 * - ui_contributions —— 插件贡献的 Widget/面板/快捷按钮
 * - ui_schema（0.1 向后兼容字段）
 * - scene 渲染空间（@deprecated 不作为独立空间，形象走 workspace widget，见 RenderingSpaceType）
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

/**
 * 渲染空间类型
 *
 * 'fullscreen' 保留向后兼容。
 *
 * @deprecated 'scene' 不作为独立空间——数字人/3D/2D 形象走 workspace 的 widget
 * （注册名 digital_human / avatar_3d 等），见 ADR §2.1 / §7.6。
 * 枚举值暂保留仅为向后兼容（已有 Schema 可能声明 scene），不再往里填新内容；
 * 渲染层会把 scene 接入 workspace 管线或忽略。新代码请勿使用 'scene'。
 */
export type RenderingSpaceType = 'chat' | 'workspace' | 'floating' | 'dock' | 'fullscreen' | 'scene'

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

// ============================================================================
// 0.2 新增：ui 字段类型（input_form / result_widget）
// ============================================================================

/**
 * 输入表单字段定义
 *
 * 描述插件/工具的输入表单结构，SchemaDriver 解析后自动生成表单 UI。
 */
export interface UIInputFormField {
  /** 字段名 */
  name: string
  /**
   * 字段类型（统一词汇表：SchemaDriver 与 FormWidget 原两套词汇已合并）
   * input/toggle/slider/color/radio/checkbox 为原 FormWidget 词汇；
   * input≈string、toggle≈boolean，RjsfForm 映射时归一
   */
  type:
    | 'string'
    | 'number'
    | 'boolean'
    | 'select'
    | 'multiselect'
    | 'textarea'
    | 'date'
    | 'file'
    | 'input'
    | 'toggle'
    | 'slider'
    | 'color'
    | 'radio'
    | 'checkbox'
  /** 标签文本 */
  label: string
  /** 描述/提示 */
  description?: string
  /** 默认值 */
  default?: unknown
  /** 是否必填 */
  required?: boolean
  /** 选择项（type 为 select/multiselect/radio/checkbox 时使用） */
  options?: Array<{ label: string; value: string | number }>
  /** 动态数据源 URI（调用内核代理端点获取选项列表） */
  datasourceUri?: string
  /**
   * 级联依赖（缺口 G2）：依赖字段值变化时本字段选项自动重拉。
   * 也可不声明——datasourceUri 里的 {{其他字段}} 模板引用会被自动推断为依赖。
   */
  dependsOn?: string[]
  /** 占位符 */
  placeholder?: string
  /** 数值范围与步长（number/slider 类型） */
  min?: number
  max?: number
  step?: number
  /** 验证规则 */
  validation?: {
    min?: number
    max?: number
    pattern?: string
    message?: string
  }
}

/**
 * 输入表单定义
 *
 * 0.2 新的 ui.input_form 字段，SchemaDriver 解析后自动生成表单 UI。
 */
export interface UIInputForm {
  /** 表单字段列表 */
  fields: UIInputFormField[]
  /** 提交按钮文本 */
  submitLabel?: string
  /** 取消按钮文本 */
  cancelLabel?: string
  /** 表单布局：单列/双列 */
  layout?: 'single' | 'double'
}

/**
 * 结果展示 Widget 定义
 *
 * 0.2 新的 ui.result_widget 字段，描述工具/能力的输出渲染方式。
 */
export interface UIResultWidget {
  /** Widget 类型标识 */
  type: string
  /** 目标渲染空间 */
  renderSpace?: RenderingSpaceType
  /** 组件属性模板 */
  props?: Record<string, unknown>
  /** 数据源引用 */
  datasourceUri?: string
  /** 自动刷新间隔（毫秒） */
  refreshInterval?: number
}

/**
 * 0.2 新的 ui 字段
 *
 * 包含 input_form 和 result_widget，是 0.2 SchemaDriver 的核心扩展点。
 */
export interface UIField {
  /** 输入表单定义 */
  inputForm?: UIInputForm
  /** 结果展示 Widget 定义 */
  resultWidget?: UIResultWidget
}

// ============================================================================
// 0.2 新增：ui_contributions 类型（插件视觉扩展）
// ============================================================================

/**
 * 插件 UI 贡献项类型
 *
 * 描述插件向前端贡献的 UI 元素类别：
 * - widget: 自定义 Widget 组件
 * - panel: Dock 面板
 * - shortcut: 快捷按钮
 * - context_menu: 右键菜单项
 * - tab: 工作区标签页
 */
export type UIContributionType = 'widget' | 'panel' | 'shortcut' | 'context_menu' | 'tab'

/**
 * 插件 UI 贡献项定义
 *
 * 第三方插件通过 manifest 的 ui_contributions 声明自定义 Widget/Tab/面板，
 * 前端 SchemaParser 解析 → RenderingEngine 按 widget_type 路由到对应渲染空间。
 */
export interface UIContribution {
  /** 贡献项类型 */
  type: UIContributionType
  /** Widget 类型标识（唯一 key，用于路由到渲染空间） */
  widgetType: string
  /** 目标渲染空间 */
  renderSpace: RenderingSpaceType
  /** 显示名称 */
  label?: string
  /** 图标 */
  icon?: string
  /** 组件属性 Schema */
  schema?: Record<string, unknown>
  /** 动态数据源 URI */
  datasourceUri?: string
  /** 排序权重（越小越靠前） */
  order?: number
  /** 是否默认展开/激活 */
  defaultActive?: boolean
}

// ============================================================================
// 0.2 新增：动态数据源类型
// ============================================================================

/**
 * 动态数据源定义
 *
 * 插件通过 manifest 声明数据源 URI，前端通过内核代理端点获取数据。
 * 典型场景：select 组件的 options 列表从后端动态获取。
 */
export interface DynamicDataSource {
  /** 数据源 URI（如 "datasource://tools/categories"） */
  uri: string
  /** 请求参数 */
  params?: Record<string, unknown>
  /** 缓存 TTL（秒） */
  cacheTtl?: number
}

/**
 * 动态数据源响应
 */
export interface DynamicDataSourceResponse {
  /** 是否成功 */
  success: boolean
  /** 选项列表 */
  options?: Array<{ label: string; value: string | number }>
  /** 原始数据 */
  data?: unknown
}

// ============================================================================
// 完整的模块 UI Schema（0.2 扩展）
// ============================================================================

/**
 * 完整的模块 UI Schema
 *
 * 0.1 字段：identity、actions、rendering、clients
 * 0.2 新增字段（均可选，渐进增强）：
 * - ui：input_form / result_widget
 * - ui_contributions：插件视觉扩展
 * - ui_schema：0.1 向后兼容字段（旧格式）
 */
export interface ModuleUISchema {
  /** 模块身份 */
  identity: ModuleIdentity
  /** 模块操作 */
  actions: ModuleAction[]
  /** 渲染配置 */
  rendering: ModuleRendering
  /** 客户端能力要求 */
  clients: ClientCapabilities
  /** 0.2 新增：UI 字段（input_form / result_widget） */
  ui?: UIField
  /** 0.2 新增：插件 UI 贡献项列表 */
  ui_contributions?: UIContribution[]
  /** 0.1 向后兼容：旧版 ui_schema 字段 */
  ui_schema?: Record<string, unknown>
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
  /** 0.2 新增：解析后的 UI 字段 */
  ui?: UIField
  /** 0.2 新增：解析后的插件 UI 贡献项 */
  ui_contributions?: UIContribution[]
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
