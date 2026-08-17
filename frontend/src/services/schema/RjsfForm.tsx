/**
 * RjsfForm — 统一表单核心（react-jsonschema-form + @rjsf/antd 主题）
 *
 * 单一表单引擎，服务所有表单入口（原三套手写渲染/校验实现已废弃）：
 * - SchemaDriver：设置页 / Agent 配置 / contribution pages（UIInputFormField[]）
 * - FormWidget：聊天交互 widget（词汇表超集 slider/color/radio/checkbox/toggle）
 * - SessionEditModal：插件贡献的 thread_fields
 *
 * 架构：字段词汇表（UIInputFormField 统一类型）→ JSON Schema + uiSchema 映射，
 * 渲染/状态/校验管线交给 RJSF（ajv + antd Form.Item）；本模块只补齐主题没有的能力：
 * - 自定义 widgets：switch（antd Switch）/ asyncSelect（datasourceUri 异步下拉）/
 *   colorPicker / filePicker
 * - transformErrors：ajv 英文错误 → 中文文案（沿用原校验文案语义）
 * - datasource 工具函数（normalizeOptions / fetchDatasourceOptions，原 SchemaDriver 迁入）
 *
 * @module RjsfForm
 */

import { useEffect, useMemo, useState } from 'react'
import { Select, Switch } from 'antd'
import Form from '@rjsf/antd'
import validator from '@rjsf/validator-ajv8'
import type { IChangeEvent } from '@rjsf/core'
import type {
  ErrorTransformer,
  RegistryWidgetsType,
  RJSFSchema,
  UiSchema,
  WidgetProps,
} from '@rjsf/utils'
import apiClient from '@/services/api/client'
import type { UIInputFormField } from '@/types/schema'

// ============================================================================
// datasource 工具（原 SchemaDriver 迁入）
// ============================================================================

/** 标准化选项 */
export interface SchemaOption {
  label: string
  value: string | number
}

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
 *
 * TODO(休眠断点登记)：后端 /api/v1/datasource/{uri} 目前以占位响应收敛（见
 * services/api/client.ts isOptionalEndpoint），插件声明 datasourceUri 的 select
 * 暂拿不到真实数据；后端代理端点落地后此处无需改动即可工作。
 */
export async function fetchDatasourceOptions(uri: string): Promise<SchemaOption[]> {
  const url = uri.startsWith('/') ? uri : `/api/v1/datasource/${uri}`
  const response = await apiClient.get(url)
  return normalizeDatasourceResponse(response.data)
}

// ============================================================================
// 字段词汇表 → JSON Schema / uiSchema 映射
// ============================================================================

/** options → oneOf（select：值字符串化，对齐原原生 select 语义） */
function optionsToStringOneOf(options: NonNullable<UIInputFormField['options']>) {
  return options.map((o) => ({ const: String(o.value), title: o.label }))
}

/** options → oneOf（multiselect/radio/checkbox：保留原始值类型） */
function optionsToRawOneOf(options: NonNullable<UIInputFormField['options']>) {
  return options.map((o) => ({ const: o.value, title: o.label }))
}

/** 数值约束来源：validation.min/max 优先，字段级 min/max 兜底（FormWidget 词汇） */
function numericBounds(field: UIInputFormField): { minimum?: number; maximum?: number } {
  const minimum = field.validation?.min ?? field.min
  const maximum = field.validation?.max ?? field.max
  return {
    ...(minimum !== undefined ? { minimum } : {}),
    ...(maximum !== undefined ? { maximum } : {}),
  }
}

/**
 * 字段定义数组 → RJSF { schema, uiSchema }
 *
 * 未知 type 回退 string；datasourceUri 字段走 asyncSelect widget（去掉枚举约束，
 * 保留 type——RJSF 对无 type 的 property 不渲染 widget；值统一字符串化）。
 */
export function toRjsf(fields: UIInputFormField[]): {
  schema: RJSFSchema
  uiSchema: UiSchema
} {
  const properties: Record<string, unknown> = {}
  const uiSchema: UiSchema = {}
  const required: string[] = []

  for (const field of fields) {
    const prop: Record<string, unknown> = { title: field.label ?? field.name }
    if (field.description) prop.description = field.description
    const ui: UiSchema = {}

    switch (field.type) {
      case 'textarea':
        prop.type = 'string'
        ui['ui:widget'] = 'textarea'
        break
      case 'number': {
        prop.type = 'number'
        Object.assign(prop, numericBounds(field))
        break
      }
      case 'boolean':
      case 'toggle':
        prop.type = 'boolean'
        ui['ui:widget'] = 'switch'
        break
      case 'select':
        prop.type = 'string'
        if (field.options?.length) prop.oneOf = optionsToStringOneOf(field.options)
        break
      case 'radio':
        // type 必须有（RJSF 对无 type 的 property 不渲染 widget），按选项值类型推断
        if (field.options?.length) {
          prop.type = typeof field.options[0].value === 'number' ? 'number' : 'string'
          prop.oneOf = optionsToRawOneOf(field.options)
        } else {
          prop.type = 'string'
        }
        ui['ui:widget'] = 'radio'
        break
      case 'multiselect':
      case 'checkbox':
        prop.type = 'array'
        prop.uniqueItems = true
        if (field.required) prop.minItems = 1
        if (field.options?.length) prop.items = { oneOf: optionsToRawOneOf(field.options) }
        ui['ui:widget'] = 'checkboxes'
        break
      case 'date':
        prop.type = 'string'
        ui['ui:widget'] = 'date'
        break
      case 'file':
        prop.type = 'string'
        ui['ui:widget'] = 'filePicker'
        break
      case 'slider': {
        prop.type = 'number'
        Object.assign(prop, numericBounds(field))
        if (field.step !== undefined) prop.multipleOf = field.step
        ui['ui:widget'] = 'range'
        break
      }
      case 'color':
        prop.type = 'string'
        ui['ui:widget'] = 'colorPicker'
        break
      case 'input':
      case 'string':
      default:
        prop.type = 'string'
        break
    }

    if (field.validation?.pattern) prop.pattern = field.validation.pattern

    // datasourceUri：覆盖为异步下拉。保留 type（RJSF 对无 type 的 property 不渲染
    // widget）；选项运行期拉取，故去掉枚举约束，值统一字符串化（对齐旧原生 select 语义）
    if (field.datasourceUri) {
      delete prop.oneOf
      delete prop.items
      if (field.type === 'multiselect' || field.type === 'checkbox') {
        prop.type = 'array'
        prop.uniqueItems = true
      } else {
        prop.type = 'string'
      }
      ui['ui:widget'] = 'asyncSelect'
      ui['ui:options'] = {
        ...(typeof ui['ui:options'] === 'object' && ui['ui:options'] !== null
          ? (ui['ui:options'] as Record<string, unknown>)
          : {}),
        datasourceUri: field.datasourceUri,
        fallbackOptions: field.options ?? [],
        multiple: field.type === 'multiselect' || field.type === 'checkbox',
      }
    }

    if (field.placeholder) ui['ui:placeholder'] = field.placeholder
    properties[field.name] = prop
    uiSchema[field.name] = ui
    if (field.required) required.push(field.name)
  }

  return {
    schema: {
      type: 'object',
      properties: properties as RJSFSchema['properties'],
      ...(required.length > 0 ? { required } : {}),
    },
    uiSchema,
  }
}

/**
 * 构建表单初始值：initialValues > 字段 default > 类型空值
 *
 * 未给的 string/number 保持 undefined（让 placeholder 可见）；数组用 []；
 * toggle 用 false；slider 用 min（默认 0）。
 */
export function buildFormValues(
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
    if (field.type === 'multiselect' || field.type === 'checkbox') {
      out[field.name] = []
    } else if (field.type === 'boolean' || field.type === 'toggle') {
      out[field.name] = false
    } else if (field.type === 'slider') {
      out[field.name] = field.min ?? 0
    }
  }
  return out
}

// ============================================================================
// 错误文案中文化（ajv → 原手写校验文案语义）
// ============================================================================

/**
 * 构造 transformErrors：把 ajv 错误翻译为中文文案。
 *
 * 文案对齐原 SchemaDriver/FormWidget 手写校验：
 * required → 「X不能为空」；pattern → validation.message || 格式不正确；
 * minimum/maximum → 「最小值为 n / 最大值为 n」；number 类型错误 → 请输入有效的数字。
 */
export function makeErrorTransformer(fields: UIInputFormField[]): ErrorTransformer {
  const byName = new Map(fields.map((f) => [f.name, f]))
  return (errors) => {
    for (const error of errors) {
      const name =
        (error.params as { missingProperty?: string } | undefined)?.missingProperty ??
        error.property?.split('.').filter(Boolean).pop()
      const field = name ? byName.get(String(name)) : undefined
      const label = field?.label ?? name
      switch (error.name) {
        case 'required':
          error.message = field ? `${label}不能为空` : '此字段为必填项'
          break
        case 'minItems':
          // 必填多选/复选：空数组
          error.message = field ? `${label}不能为空` : '至少选择一项'
          break
        case 'pattern':
          error.message = field?.validation?.message || '格式不正确'
          break
        case 'minimum':
          error.message = `最小值为 ${(error.params as { limit?: number } | undefined)?.limit}`
          break
        case 'maximum':
          error.message = `最大值为 ${(error.params as { limit?: number } | undefined)?.limit}`
          break
        case 'type':
          if (field && (field.type === 'number' || field.type === 'slider')) {
            error.message = '请输入有效的数字'
          }
          break
      }
    }
    return errors
  }
}

// ============================================================================
// 自定义 widgets（@rjsf/antd 主题未覆盖的能力）
// ============================================================================

/** toggle/boolean → antd Switch */
function SwitchWidget({ id, value, onChange, disabled, readonly }: WidgetProps) {
  return (
    <Switch
      id={id}
      checked={Boolean(value)}
      disabled={disabled || readonly}
      onChange={(checked) => onChange(checked)}
    />
  )
}

/** datasourceUri 异步下拉：挂载拉取选项，失败回退静态 options */
function AsyncSelectWidget(props: WidgetProps) {
  const { id, value, onChange, disabled, readonly, placeholder, options } = props
  const opts = options as unknown as {
    datasourceUri?: string
    fallbackOptions?: Array<{ label: string; value: string | number }>
    multiple?: boolean
  }
  const uri = opts.datasourceUri ?? ''
  const multiple = Boolean(opts.multiple)
  const [asyncOptions, setAsyncOptions] = useState<Array<{ label: string; value: string | number }>>(
    [],
  )
  const [loading, setLoading] = useState(Boolean(uri))

  useEffect(() => {
    if (!uri) return
    let cancelled = false
    setLoading(true)
    fetchDatasourceOptions(uri)
      .then((fetched) => {
        if (!cancelled) setAsyncOptions(fetched)
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
  }, [uri])

  const finalOptions = asyncOptions.length > 0 ? asyncOptions : (opts.fallbackOptions ?? [])

  return (
    <Select
      id={id}
      mode={multiple ? 'multiple' : undefined}
      style={{ width: '100%' }}
      disabled={disabled || readonly}
      loading={loading}
      placeholder={placeholder ?? '请选择'}
      value={value === undefined ? undefined : value}
      onChange={(next) => {
        if (multiple) {
          onChange(Array.isArray(next) ? next.map((v) => String(v)) : [String(next)])
        } else {
          onChange(next === undefined || next === null ? undefined : String(next))
        }
      }}
      options={finalOptions.map((o) => ({ label: o.label, value: o.value }))}
    />
  )
}

/** color → 原生取色器 + 十六进制回显 */
function ColorPickerWidget({ id, value, onChange, disabled, readonly }: WidgetProps) {
  const hex = String(value ?? '#000000')
  return (
    <div className="flex items-center gap-2">
      <input
        id={id}
        type="color"
        className="h-9 w-12 cursor-pointer rounded border-0 p-0"
        value={hex}
        disabled={disabled || readonly}
        onChange={(e) => onChange(e.target.value)}
      />
      <span className="text-muted-foreground text-sm">{hex}</span>
    </div>
  )
}

/** file → 原生文件选择（表单值存文件名） */
function FilePickerWidget({ id, value, onChange, disabled, readonly }: WidgetProps) {
  return (
    <input
      id={id}
      type="file"
      className="bg-background border-input w-full rounded-md border px-3 py-2 text-sm"
      value={undefined}
      disabled={disabled || readonly}
      onChange={(e) => onChange(e.target.files?.[0]?.name ?? '')}
      data-value={value}
    />
  )
}

/** 自定义 widget 注册表（与 @rjsf/antd 主题 widgets 合并） */
const WIDGETS: RegistryWidgetsType = {
  switch: SwitchWidget,
  asyncSelect: AsyncSelectWidget,
  colorPicker: ColorPickerWidget,
  filePicker: FilePickerWidget,
}

/** 顶部错误清单关闭（错误就近显示在 Form.Item，避免重复） */
function NoopErrorList(): null {
  return null
}

// ============================================================================
// RjsfForm 组件
// ============================================================================

/** RjsfForm 组件属性 */
export interface RjsfFormProps {
  /** 字段定义（UIInputFormField 统一词汇表） */
  fields: UIInputFormField[]
  /** 初始值（编辑场景） */
  initialValues?: Record<string, unknown>
  /**
   * 提交回调（可返回 Promise，提交期间按钮 loading）。
   * 不传时表单为字段模式：不渲染提交按钮，值经 onChange 流出
   */
  onSubmit?: (values: Record<string, unknown>) => void | Promise<void>
  /** 值变更回调（字段模式下消费表单值） */
  onChange?: (values: Record<string, unknown>) => void
  /** 提交按钮文案 */
  submitLabel?: string
  /** 表单标题 */
  title?: string
  /** 布局：single 单列 / double 双列（antd Row/Col colSpan 24/12） */
  layout?: 'single' | 'double'
  /** 整表禁用（只读展示场景，如工具 output_schema 结构化视图） */
  disabled?: boolean
}

/**
 * 统一动态表单组件
 *
 * @param props - 字段定义、初始值、提交/变更回调、布局
 * @returns RJSF + antd 主题渲染的动态表单
 */
export function RjsfForm({
  fields,
  initialValues,
  onSubmit,
  onChange,
  submitLabel = '提交',
  title,
  layout = 'single',
  disabled,
}: RjsfFormProps) {
  const [submitting, setSubmitting] = useState(false)
  const { schema, uiSchema: fieldUiSchema } = useMemo(() => toRjsf(fields), [fields])
  const [formData] = useState(() => buildFormValues(fields, initialValues))
  const transformErrors = useMemo(() => makeErrorTransformer(fields), [fields])

  const uiSchema = useMemo(
    () => ({
      ...fieldUiSchema,
      'ui:submitButtonOptions': {
        norender: !onSubmit,
        submitText: submitting ? '提交中...' : submitLabel,
        props: { type: 'primary' as const, loading: submitting, disabled: submitting },
      },
    }),
    [fieldUiSchema, submitLabel, onSubmit, submitting],
  )

  if (fields.length === 0) {
    return (
      <div className="rounded-lg border border-dashed p-6 text-center">
        <p className="text-muted-foreground text-sm">暂无表单字段</p>
      </div>
    )
  }

  const handleSubmit = async ({ formData: values }: IChangeEvent) => {
    if (!onSubmit) return
    setSubmitting(true)
    try {
      await onSubmit(values ?? {})
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="space-y-4">
      {title && <h3 className="text-foreground text-base font-semibold">{title}</h3>}
      <Form
        schema={schema}
        uiSchema={uiSchema}
        validator={validator}
        formData={formData}
        formContext={{ colSpan: layout === 'double' ? 12 : 24 }}
        widgets={WIDGETS}
        templates={{ ErrorListTemplate: NoopErrorList }}
        transformErrors={transformErrors}
        omitExtraData
        disabled={disabled}
        onSubmit={handleSubmit}
        onChange={
          onChange ? ({ formData: values }: IChangeEvent) => onChange(values ?? {}) : undefined
        }
      />
    </div>
  )
}

export default RjsfForm
