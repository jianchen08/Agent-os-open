/**
 * UI Schema 类型定义
 *
 * SchemaDriver 解析结果的类型面：渲染空间词汇、ui 字段
 * （input_form / result_widget）与动态数据源引用。
 */

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
    | 'directory'
    | 'input'
    | 'toggle'
    | 'slider'
    | 'color'
    | 'radio'
    | 'checkbox'
  /** 标签文本（缺省回退 name；compact 选择器还回退 props.title） */
  label?: string
  /** 描述/提示 */
  description?: string
  /** 默认值 */
  default?: unknown
  /** 是否必填 */
  required?: boolean
  /**
   * 条件必填：依赖字段的值等于 equals 时本字段必填（提交期校验）。
   * 如触发器表单按 trigger_type 要求类型专属参数（delay 类型必填 delay_seconds）。
   */
  requiredWhen?: { field: string; equals: string | number }
  /** 选择项（type 为 select/multiselect/radio/checkbox 时使用） */
  options?: Array<{ label: string; value: string | number }>
  /**
   * 值守卫渲染（与插件声明 x_guard 同源语义）：requires 指向的另一字段为空时，
   * 本字段选项中除 onEmpty 外全部置灰不可选（如工作空间拓扑 worktree 依赖
   * 工作空间目录已填写；选项 label 自带的说明文案即悬浮 title）。空判定与
   * applyGuards 一致：依赖值非字符串或去空格后为空。
   */
  optionGuard?: { requires: string; onEmpty: string | number }
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

// ============================================================================
// 动态数据源类型
// ============================================================================

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
