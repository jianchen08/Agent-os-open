/**
 * 表单交互组件
 *
 * 根据 Schema 渲染动态表单，支持多种字段类型和布局。
 * 字段类型：input、textarea、select、toggle、number、slider、color
 *
 * @module FormWidget
 */

import React, { useState, useCallback } from 'react'

/** 表单字段定义 */
interface FormField {
  /** 字段标识 */
  name: string
  /** 字段类型 */
  type: 'input' | 'textarea' | 'select' | 'toggle' | 'number' | 'slider' | 'color'
  /** 字段标签 */
  label?: string
  /** 占位文本 */
  placeholder?: string
  /** 是否必填 */
  required?: boolean
  /** 默认值 */
  default?: unknown
  /** 校验规则 */
  validation?: {
    min?: number
    max?: number
    pattern?: string
    message?: string
  }
  /** 选项列表（select 类型使用） */
  options?: Array<{ value: string; label: string }>
  /** 最小值（number/slider 类型） */
  min?: number
  /** 最大值（number/slider 类型） */
  max?: number
  /** 步长（number/slider 类型） */
  step?: number
}

/** 表单布局类型 */
type FormLayout = 'horizontal' | 'vertical' | 'grid'

/** 校验错误映射 */
type ValidationErrors = Record<string, string>

/**
 * 提取安全的字段数组
 *
 * @param fields - 原始字段定义
 * @returns 类型安全的 FormField 数组
 */
function extractFields(fields: unknown): FormField[] {
  if (!Array.isArray(fields)) return []
  return fields.filter(
    (f): f is FormField =>
      typeof f === 'object' && f !== null && typeof (f as FormField).name === 'string',
  )
}

/**
 * 提取 select 字段的选项
 *
 * @param options - 原始选项
 * @returns 标准化选项数组
 */
function extractOptions(
  options: unknown,
): Array<{ value: string; label: string }> {
  if (!Array.isArray(options)) return []
  return options.filter(
    (o): o is { value: string; label: string } =>
      typeof o === 'object' && o !== null,
  )
}

/**
 * 校验单个字段
 *
 * @param field - 字段定义
 * @param value - 当前值
 * @returns 错误消息，无错误时返回空字符串
 */
function validateField(field: FormField, value: unknown): string {
  if (field.required && (value === undefined || value === null || value === '')) {
    return field.label ? `${field.label}不能为空` : '此字段为必填项'
  }

  if (value === undefined || value === null || value === '') return ''

  const strValue = String(value)
  const validation = field.validation

  if (validation?.pattern) {
    const regex = new RegExp(validation.pattern)
    if (!regex.test(strValue)) {
      return validation.message || '格式不正确'
    }
  }

  if (field.type === 'number' || field.type === 'slider') {
    const numValue = Number(value)
    if (isNaN(numValue)) return '请输入有效的数字'
    if (field.min !== undefined && numValue < field.min) {
      return `最小值为 ${field.min}`
    }
    if (field.max !== undefined && numValue > field.max) {
      return `最大值为 ${field.max}`
    }
    if (validation?.min !== undefined && numValue < validation.min) {
      return `最小值为 ${validation.min}`
    }
    if (validation?.max !== undefined && numValue > validation.max) {
      return `最大值为 ${validation.max}`
    }
  }

  return ''
}

/**
 * 表单交互组件
 *
 * 支持多种字段类型、布局模式和表单校验。
 *
 * @param props - 组件属性，包含 fields、layout、onSubmit 等
 * @returns 动态表单渲染结果
 */
export function FormWidget(props: Record<string, unknown>) {
  const fields = extractFields(props.fields)
  const layout = (props.layout as FormLayout) ?? 'vertical'
  const onSubmit = props.onSubmit as
    | ((data: Record<string, unknown>) => void)
    | undefined
  const submitLabel = (props.submitLabel as string) ?? '提交'
  const title = props.title as string | undefined

  // 初始化表单值
  const initialValues: Record<string, unknown> = {}
  for (const field of fields) {
    initialValues[field.name] = field.default ?? ''
  }

  const [values, setValues] = useState<Record<string, unknown>>(initialValues)
  const [errors, setErrors] = useState<ValidationErrors>({})
  const [submitting, setSubmitting] = useState(false)

  const handleChange = useCallback(
    (fieldName: string, value: unknown) => {
      setValues((prev) => ({ ...prev, [fieldName]: value }))
      setErrors((prev) => {
        const next = { ...prev }
        delete next[fieldName]
        return next
      })
    },
    [],
  )

  const handleSubmit = useCallback(() => {
    const newErrors: ValidationErrors = {}
    for (const field of fields) {
      const error = validateField(field, values[field.name])
      if (error) {
        newErrors[field.name] = error
      }
    }

    if (Object.keys(newErrors).length > 0) {
      setErrors(newErrors)
      return
    }

    if (onSubmit) {
      setSubmitting(true)
      try {
        onSubmit(values)
      } finally {
        setSubmitting(false)
      }
    }
  }, [fields, values, onSubmit])

  const layoutClass =
    layout === 'horizontal'
      ? 'flex flex-wrap gap-4'
      : layout === 'grid'
        ? 'grid grid-cols-2 gap-4'
        : 'flex flex-col gap-4'

  if (fields.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-6 text-center">
        <p className="text-muted-foreground text-sm">暂无表单字段</p>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      {title && (
        <h3 className="text-foreground text-base font-semibold">{title}</h3>
      )}

      <div className={layoutClass}>
        {fields.map((field) => (
          <div
            key={field.name}
            className={
              layout === 'grid' && field.type === 'textarea' ? 'col-span-2' : ''
            }
          >
            <label className="text-foreground mb-1 block text-sm font-medium">
              {field.label ?? field.name}
              {field.required && <span className="text-status-error ml-1">*</span>}
            </label>

            {renderFieldInput(field, values[field.name], errors[field.name], handleChange)}

            {errors[field.name] && (
              <p className="mt-1 text-xs text-status-error">{errors[field.name]}</p>
            )}
          </div>
        ))}
      </div>

      <button
        type="button"
        onClick={handleSubmit}
        disabled={submitting}
        className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-md px-4 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50"
      >
        {submitting ? '提交中...' : submitLabel}
      </button>
    </div>
  )
}

/**
 * 渲染单个字段的输入控件
 *
 * @param field - 字段定义
 * @param value - 当前值
 * @param error - 校验错误
 * @param onChange - 值变更回调
 * @returns 字段输入控件 JSX
 */
function renderFieldInput(
  field: FormField,
  value: unknown,
  _error: string | undefined,
  onChange: (name: string, value: unknown) => void,
): React.ReactNode {
  const baseClass =
    'bg-background border-input w-full rounded-md border px-3 py-2 text-sm transition-colors focus:outline-none focus:ring-2 focus:ring-ring'

  switch (field.type) {
    case 'textarea':
      return (
        <textarea
          className={`${baseClass} min-h-[80px] resize-y`}
          placeholder={field.placeholder}
          value={String(value ?? '')}
          onChange={(e) => onChange(field.name, e.target.value)}
        />
      )

    case 'select': {
      const options = extractOptions(field.options)
      return (
        <select
          className={baseClass}
          value={String(value ?? '')}
          onChange={(e) => onChange(field.name, e.target.value)}
        >
          <option value="">{field.placeholder ?? '请选择'}</option>
          {options.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      )
    }

    case 'toggle':
      return (
        <button
          type="button"
          role="switch"
          aria-checked={Boolean(value)}
          onClick={() => onChange(field.name, !value)}
          className={`relative h-6 w-11 rounded-full transition-colors ${
            value ? 'bg-primary' : 'bg-muted'
          }`}
        >
          <span
            className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${
              value ? 'translate-x-5' : 'translate-x-0'
            }`}
          />
        </button>
      )

    case 'number':
      return (
        <input
          type="number"
          className={baseClass}
          placeholder={field.placeholder}
          min={field.min}
          max={field.max}
          step={field.step ?? 1}
          value={value !== '' && value !== undefined ? Number(value) : ''}
          onChange={(e) => onChange(field.name, e.target.value ? Number(e.target.value) : '')}
        />
      )

    case 'slider':
      return (
        <div className="flex items-center gap-3">
          <input
            type="range"
            className="bg-primary h-2 w-full cursor-pointer appearance-none rounded-lg accent-primary"
            min={field.min ?? 0}
            max={field.max ?? 100}
            step={field.step ?? 1}
            value={Number(value ?? 0)}
            onChange={(e) => onChange(field.name, Number(e.target.value))}
          />
          <span className="text-foreground w-10 text-right text-sm tabular-nums">
            {String(value ?? 0)}
          </span>
        </div>
      )

    case 'color':
      return (
        <div className="flex items-center gap-2">
          <input
            type="color"
            className="h-9 w-12 cursor-pointer rounded border-0 p-0"
            value={String(value ?? '#000000')}
            onChange={(e) => onChange(field.name, e.target.value)}
          />
          <span className="text-muted-foreground text-sm">
            {String(value ?? '#000000')}
          </span>
        </div>
      )

    case 'input':
    default:
      return (
        <input
          type="text"
          className={baseClass}
          placeholder={field.placeholder}
          value={String(value ?? '')}
          onChange={(e) => onChange(field.name, e.target.value)}
        />
      )
  }
}
