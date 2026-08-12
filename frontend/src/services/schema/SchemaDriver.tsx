/**
 * SchemaDriver 表单驱动组件
 *
 * 消费后端字段级 Schema（`UIInputFormField[]`，来源 GET /api/v1/agents/schema）
 * 自动生成表单：
 * - string → 文本输入 / textarea → 多行文本 / number → 数字输入 / boolean → 复选
 * - select → 下拉（options 静态枚举 或 datasourceUri 动态拉取）
 * - multiselect → 复选框组（值数组）
 * - date → date 输入 / file → 文件选择（不入表单值）
 * - datasourceUri：字段声明动态数据源时，挂载即经 apiClient 拉取选项；
 *   绝对 URI（以 / 开头）直连，其余走 /api/v1/datasource/{uri} 代理端点
 * - required / min / max / pattern 客户端校验
 *
 * 不依赖 antd（保持与 FormWidget 一致的原生控件风格，便于测试与样式统一）。
 *
 * @module SchemaDriver
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react'
import apiClient from '@/services/api/client'
import type { UIInputFormField } from '@/types/schema'

/** 标准化选项 */
export interface SchemaOption {
  label: string
  value: string | number
}

/** SchemaDriver 组件属性 */
export interface SchemaDriverProps {
  /** 字段定义（UIInputFormField[]） */
  fields: UIInputFormField[]
  /** 初始值（编辑场景：从 yaml/JSON 解析出的对象） */
  initialValues?: Record<string, unknown>
  /** 提交回调（可返回 Promise，提交期间按钮显示 loading） */
  onSubmit: (values: Record<string, unknown>) => void | Promise<void>
  /** 提交按钮文案 */
  submitLabel?: string
  /** 表单标题（可选） */
  title?: string
  /** 布局：single 单列 / double 双列 */
  layout?: 'single' | 'double'
}

/** 校验错误映射 */
type ValidationErrors = Record<string, string>

/**
 * 标准化选项数组
 *
 * 兼容多种来源格式：{label, value} / 纯字符串 / 纯数字。
 */
export function normalizeOptions(options: unknown): SchemaOption[] {
  if (!Array.isArray(options)) return []
  const result: SchemaOption[] = []
  for (const item of options) {
    if (typeof item === 'string' || typeof item === 'number') {
      result.push({ label: String(item), value: item })
    } else if (item && typeof item === 'object') {
      const rec = item as { label?: unknown; value?: unknown }
      if (rec.value !== undefined) {
        result.push({
          label: rec.label !== undefined ? String(rec.label) : String(rec.value),
          value: rec.value as string | number,
        })
      }
    }
  }
  return result
}

/**
 * 归一化动态数据源响应
 *
 * 兼容三种形态：`{ options: [...] }` / `{ data: [...] }` / 直接数组。
 */
function normalizeDatasourceResponse(data: unknown): SchemaOption[] {
  if (Array.isArray(data)) return normalizeOptions(data)
  if (data && typeof data === 'object') {
    const rec = data as Record<string, unknown>
    if (Array.isArray(rec.options)) return normalizeOptions(rec.options)
    if (Array.isArray(rec.data)) return normalizeOptions(rec.data)
  }
  return []
}

/**
 * 拉取动态数据源选项
 *
 * @param uri - 数据源 URI：以 / 开头的绝对路径直连；否则走 /api/v1/datasource/{uri} 代理
 */
export async function fetchDatasourceOptions(uri: string): Promise<SchemaOption[]> {
  const url = uri.startsWith('/') ? uri : `/api/v1/datasource/${uri}`
  const response = await apiClient.get(url)
  return normalizeDatasourceResponse(response.data)
}

/**
 * 构建表单初始值：initialValues > 字段 default > 类型空值
 */
function buildInitialValues(
  fields: UIInputFormField[],
  initialValues: Record<string, unknown> | undefined,
): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const field of fields) {
    const fromInitial = initialValues?.[field.name]
    if (fromInitial !== undefined && fromInitial !== null) {
      out[field.name] = fromInitial
      continue
    }
    if (field.default !== undefined && field.default !== null) {
      out[field.name] = field.default
      continue
    }
    out[field.name] = field.type === 'multiselect' ? [] : ''
  }
  return out
}

/**
 * 校验单个字段
 *
 * @param field - 字段定义
 * @param value - 当前值
 * @returns 错误消息，无错误时返回空字符串
 */
export function validateSchemaField(field: UIInputFormField, value: unknown): string {
  const isEmpty =
    value === undefined ||
    value === null ||
    value === '' ||
    (Array.isArray(value) && value.length === 0)

  if (field.required && isEmpty) {
    return field.label ? `${field.label}不能为空` : '此字段为必填项'
  }
  if (isEmpty) return ''

  if (field.type === 'number') {
    const num = Number(value)
    if (Number.isNaN(num)) return '请输入有效的数字'
    if (field.validation?.min !== undefined && num < field.validation.min) {
      return `最小值为 ${field.validation.min}`
    }
    if (field.validation?.max !== undefined && num > field.validation.max) {
      return `最大值为 ${field.validation.max}`
    }
  }

  if (field.validation?.pattern) {
    const regex = new RegExp(field.validation.pattern)
    if (!regex.test(String(value))) {
      return field.validation.message || '格式不正确'
    }
  }

  return ''
}

/**
 * 单个字段控件
 *
 * 独立组件以便安全使用 useDatasourceOptions hook（datasourceUri 动态选项）。
 */
function FieldControl({
  field,
  value,
  onChange,
}: {
  field: UIInputFormField
  value: unknown
  onChange: (name: string, value: unknown) => void
}) {
  const { options: dynamicOptions, loading } = useDatasourceOptions(field)

  const options = useMemo(() => {
    if (dynamicOptions.length > 0) return dynamicOptions
    return normalizeOptions(field.options)
  }, [dynamicOptions, field.options])

  const baseClass =
    'bg-background border-input w-full rounded-md border px-3 py-2 text-sm transition-colors focus:outline-none focus:ring-2 focus:ring-ring'

  switch (field.type) {
    case 'textarea':
      return (
        <textarea
          id={`schema-field-${field.name}`}
          className={`${baseClass} min-h-[80px] resize-y`}
          placeholder={field.placeholder}
          value={String(value ?? '')}
          onChange={(e) => onChange(field.name, e.target.value)}
        />
      )

    case 'select': {
      if (loading) {
        return (
          <select id={`schema-field-${field.name}`} className={baseClass} disabled>
            <option>加载中...</option>
          </select>
        )
      }
      return (
        <select
          id={`schema-field-${field.name}`}
          className={baseClass}
          value={String(value ?? '')}
          onChange={(e) => onChange(field.name, e.target.value)}
        >
          <option value="">{field.placeholder ?? '请选择'}</option>
          {options.map((opt) => (
            <option key={String(opt.value)} value={String(opt.value)}>
              {opt.label}
            </option>
          ))}
        </select>
      )
    }

    case 'multiselect': {
      if (loading) {
        return <div className="text-muted-foreground text-xs">加载中...</div>
      }
      if (options.length === 0) {
        return <div className="text-muted-foreground text-xs">暂无选项</div>
      }
      const current = Array.isArray(value) ? (value as Array<string | number>) : []
      return (
        <div data-testid={`multiselect-${field.name}`} className="flex flex-wrap gap-x-4 gap-y-2">
          {options.map((opt) => {
            const checked = current.includes(opt.value)
            return (
              <label
                key={String(opt.value)}
                className="text-foreground flex cursor-pointer items-center gap-1.5 text-sm"
              >
                <input
                  type="checkbox"
                  className="border-input h-4 w-4 rounded"
                  checked={checked}
                  onChange={(e) => {
                    const next = e.target.checked
                      ? [...current, opt.value]
                      : current.filter((v) => v !== opt.value)
                    onChange(field.name, next)
                  }}
                />
                {opt.label}
              </label>
            )
          })}
        </div>
      )
    }

    case 'number':
      return (
        <input
          id={`schema-field-${field.name}`}
          type="number"
          className={baseClass}
          placeholder={field.placeholder}
          min={field.validation?.min}
          max={field.validation?.max}
          value={value === '' || value === undefined || value === null ? '' : Number(value)}
          onChange={(e) =>
            onChange(field.name, e.target.value === '' ? '' : Number(e.target.value))
          }
        />
      )

    case 'date':
      return (
        <input
          id={`schema-field-${field.name}`}
          type="date"
          className={baseClass}
          value={String(value ?? '')}
          onChange={(e) => onChange(field.name, e.target.value)}
        />
      )

    case 'boolean':
      return (
        <input
          id={`schema-field-${field.name}`}
          type="checkbox"
          className="border-input h-4 w-4 rounded"
          checked={Boolean(value)}
          onChange={(e) => onChange(field.name, e.target.checked)}
        />
      )

    case 'file':
      return (
        <input
          id={`schema-field-${field.name}`}
          type="file"
          className={`${baseClass} file:mr-2 file:rounded file:border-0 file:bg-primary/10 file:px-2 file:py-1 file:text-xs`}
          placeholder={field.placeholder}
        />
      )

    case 'string':
    default:
      return (
        <input
          id={`schema-field-${field.name}`}
          type="text"
          className={baseClass}
          placeholder={field.placeholder}
          value={String(value ?? '')}
          onChange={(e) => onChange(field.name, e.target.value)}
        />
      )
  }
}

/**
 * datasourceUri 动态选项 hook
 *
 * 字段声明 datasourceUri 时挂载拉取选项；失败静默回退静态 options。
 */
function useDatasourceOptions(field: UIInputFormField): {
  options: SchemaOption[]
  loading: boolean
} {
  const [options, setOptions] = useState<SchemaOption[]>([])
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!field.datasourceUri) return
    let cancelled = false
    setLoading(true)
    fetchDatasourceOptions(field.datasourceUri)
      .then((opts) => {
        if (!cancelled) setOptions(opts)
      })
      .catch(() => {
        /* 拉取失败回退静态 options，不阻断表单 */
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [field.datasourceUri])

  return { options, loading }
}

/**
 * SchemaDriver 表单驱动组件
 *
 * @param props - 字段定义、初始值、提交回调等
 * @returns 动态表单
 */
export function SchemaDriver({
  fields,
  initialValues,
  onSubmit,
  submitLabel = '提交',
  title,
  layout = 'single',
}: SchemaDriverProps) {
  const [values, setValues] = useState<Record<string, unknown>>(() =>
    buildInitialValues(fields, initialValues),
  )
  const [errors, setErrors] = useState<ValidationErrors>({})
  const [submitting, setSubmitting] = useState(false)

  const handleChange = useCallback((fieldName: string, value: unknown) => {
    setValues((prev) => ({ ...prev, [fieldName]: value }))
    setErrors((prev) => {
      const next = { ...prev }
      delete next[fieldName]
      return next
    })
  }, [])

  const handleSubmit = useCallback(async () => {
    const newErrors: ValidationErrors = {}
    for (const field of fields) {
      const error = validateSchemaField(field, values[field.name])
      if (error) newErrors[field.name] = error
    }
    if (Object.keys(newErrors).length > 0) {
      setErrors(newErrors)
      return
    }
    setSubmitting(true)
    try {
      await onSubmit(values)
    } finally {
      setSubmitting(false)
    }
  }, [fields, values, onSubmit])

  if (fields.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-6 text-center">
        <p className="text-muted-foreground text-sm">暂无表单字段</p>
      </div>
    )
  }

  const layoutClass = layout === 'double' ? 'grid grid-cols-1 gap-4 sm:grid-cols-2' : 'flex flex-col gap-4'

  return (
    <div className="space-y-4">
      {title && <h3 className="text-foreground text-base font-semibold">{title}</h3>}

      <div className={layoutClass}>
        {fields.map((field) => (
          <div
            key={field.name}
            data-testid={`schema-field-${field.name}`}
            className={
              layout === 'double' && (field.type === 'textarea' || field.type === 'file')
                ? 'sm:col-span-2'
                : ''
            }
          >
            <label
              htmlFor={`schema-field-${field.name}`}
              className="text-foreground mb-1 block text-sm font-medium"
            >
              {field.label ?? field.name}
              {field.required && <span className="text-status-error ml-1">*</span>}
            </label>

            <FieldControl field={field} value={values[field.name]} onChange={handleChange} />

            {field.description && (
              <p className="text-muted-foreground mt-1 text-xs">{field.description}</p>
            )}
            {errors[field.name] && (
              <p className="mt-1 text-xs text-status-error">{errors[field.name]}</p>
            )}
          </div>
        ))}
      </div>

      <button
        type="button"
        data-testid="schema-submit"
        onClick={handleSubmit}
        disabled={submitting}
        className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-md px-4 py-2 text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50"
      >
        {submitting ? '提交中...' : submitLabel}
      </button>
    </div>
  )
}

export default SchemaDriver
