/**
 * ConfigModal 类型定义
 *
 * 定义配置模态框的字段类型和属性接口
 */

/**
 * 配置字段类型
 */
export type ConfigFieldType =
  | 'text'
  | 'number'
  | 'select'
  | 'textarea'
  | 'checkbox'
  | 'password'
  | 'json'

/**
 * 配置字段定义
 */
export interface ConfigField<T = object> {
  /** 字段键名 */
  key: keyof T
  /** 显示标签 */
  label: string
  /** 字段类型 */
  type: ConfigFieldType
  /** 占位符文本 */
  placeholder?: string
  /** 是否必填 */
  required?: boolean
  /** 选择框选项 */
  options?: { value: string; label: string }[]
  /** 最小值（数字类型） */
  min?: number
  /** 最大值（数字类型） */
  max?: number
  /** 步进值（数字类型） */
  step?: number
  /** 字段描述 */
  description?: string
  /** 自定义验证函数 */
  validate?: (value: unknown, formData: T) => string | null
  /** 是否禁用 */
  disabled?: boolean
  /** 文本域行数 */
  rows?: number
}

/**
 * 配置模态框属性
 */
export interface ConfigModalProps<T = object> {
  /** 是否显示模态框 */
  open: boolean
  /** 关闭回调 */
  onClose: () => void
  /** 保存回调 */
  onSave: (config: T) => Promise<void>
  /** 模态框标题 */
  title: string
  /** 字段定义列表 */
  fields: ConfigField<T>[]
  /** 当前配置数据 */
  config: T
  /** 加载状态 */
  loading?: boolean
  /** 最大宽度 */
  maxWidth?: 'sm' | 'md' | 'lg' | 'xl' | '2xl' | '3xl' | '4xl' | 'full'
  /** 配置类型（用于 localStorage 暂存） */
  configType?: string
  /** 是否显示重置按钮 */
  showReset?: boolean
  /** 自定义底部内容 */
  footer?: React.ReactNode
  /** 自定义类名 */
  className?: string
  /** 子元素（用于自定义内容） */
  children?: React.ReactNode
  /** 是否要求 isDirty 才能提交（默认 true） */
  requireDirty?: boolean
  /** 提交按钮文字（默认"保存"） */
  submitText?: string
}

/**
 * 表单错误信息
 */
export type FormErrors<T = object> = Partial<Record<keyof T, string>>

/**
 * 表单状态
 */
export interface FormState<T = object> {
  /** 表单数据 */
  data: T
  /** 错误信息 */
  errors: FormErrors<T>
  /** 是否已修改 */
  isDirty: boolean
  /** 是否正在提交 */
  isSubmitting: boolean
}
