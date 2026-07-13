/**
 * 通用模块配置渲染器
 *
 * 根据 Schema 的 config_panel 定义动态渲染配置表单
 */

import React from 'react'
import type { ModuleUISchema } from '@/types/schema'

/** 配置面板字段定义 */
interface ConfigField {
  /** 字段键名 */
  key: string
  /** 字段标签 */
  label: string
  /** 字段类型 */
  type: 'select' | 'input' | 'toggle' | 'number' | 'color'
  /** 选项列表（仅 select 类型） */
  options?: Array<{ label: string; value: string }>
  /** 默认值 */
  default?: unknown
  /** 字段描述 */
  description?: string
}

/** 模块配置渲染器属性 */
interface ModuleConfigRendererProps {
  /** 模块 Schema */
  schema: ModuleUISchema
  /** 配置值 */
  values: Record<string, unknown>
  /** 配置变更回调 */
  onChange: (key: string, value: unknown) => void
  /** 是否只读 */
  readOnly?: boolean
}

/**
 * 根据 Schema 渲染模块配置表单
 *
 * @param props - 渲染器属性，包含 schema、values、onChange、readOnly
 * @returns 动态渲染的配置表单
 */
export function ModuleConfigRenderer({
  schema,
  values,
  onChange,
  readOnly,
}: ModuleConfigRendererProps) {
  const configPanel = (schema as Record<string, unknown>).config_panel as ConfigField[] | undefined

  if (!configPanel || configPanel.length === 0) {
    return <div className="text-muted-foreground text-sm">此模块无可配置项</div>
  }

  return (
    <div className="space-y-4">
      {configPanel.map((field) => (
        <div key={field.key} className="space-y-1">
          <label className="text-sm font-medium">{field.label}</label>
          {field.description && (
            <p className="text-muted-foreground text-xs">{field.description}</p>
          )}
          {renderField(field, values[field.key] ?? field.default, onChange, readOnly)}
        </div>
      ))}
    </div>
  )
}

/**
 * 渲染单个配置字段
 *
 * @param field - 字段定义
 * @param value - 当前值
 * @param onChange - 变更回调
 * @param readOnly - 是否只读
 * @returns 对应类型的表单控件
 */
function renderField(
  field: ConfigField,
  value: unknown,
  onChange: (key: string, value: unknown) => void,
  readOnly?: boolean,
) {
  const disabled = readOnly

  switch (field.type) {
    case 'select':
      return (
        <select
          className="bg-background w-full rounded-md border px-3 py-2 text-sm"
          value={String(value ?? '')}
          onChange={(e) => onChange(field.key, e.target.value)}
          disabled={disabled}
        >
          {field.options?.map((opt) => (
            <option key={opt.value} value={opt.value}>
              {opt.label}
            </option>
          ))}
        </select>
      )

    case 'toggle':
      return (
        <button
          type="button"
          role="switch"
          aria-checked={Boolean(value)}
          className={`relative inline-flex h-6 w-11 items-center rounded-full transition-colors ${value ? 'bg-primary' : 'bg-muted'}`}
          onClick={() => onChange(field.key, !value)}
          disabled={disabled}
        >
          <span
            className={`inline-block h-4 w-4 transform rounded-full bg-white transition-transform ${value ? 'translate-x-6' : 'translate-x-1'}`}
          />
        </button>
      )

    case 'number':
      return (
        <input
          type="number"
          className="bg-background w-full rounded-md border px-3 py-2 text-sm"
          value={Number(value ?? 0)}
          onChange={(e) => onChange(field.key, Number(e.target.value))}
          disabled={disabled}
        />
      )

    case 'color':
      return (
        <input
          type="color"
          className="h-10 w-20 rounded-md border"
          value={String(value ?? '#000000')}
          onChange={(e) => onChange(field.key, e.target.value)}
          disabled={disabled}
        />
      )

    case 'input':
    default:
      return (
        <input
          type="text"
          className="bg-background w-full rounded-md border px-3 py-2 text-sm"
          value={String(value ?? '')}
          onChange={(e) => onChange(field.key, e.target.value)}
          disabled={disabled}
        />
      )
  }
}
