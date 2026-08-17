/**
 * config_files.fields 声明 → RJSF 表单字段转换（widget 化 T1）。
 *
 * 内核对 fields 只保底解析 name/label/type/required/description，其余 UI 词汇
 * （options/min/max/step/default/datasourceUri…）flatten 透传——本模块在前端
 * 收敛为 UIInputFormField 词汇表：无效条目丢弃（宁可不渲染也不渲染错控件）。
 *
 * 字段 name 支持点号路径（如 `defaults.chat`）：初值按路径从配置树读取，
 * 提交按路径写回（未声明键原样保留——fields 只声明要类型化编辑的子段）。
 */
import type { EnvConfigFieldDef } from '@/services/api/pluginConfig'
import type { UIInputFormField } from '@/types/schema'

/** UIInputFormField.type 词汇白名单（声明值不在表内 → 兜底 string） */
const FIELD_TYPES = new Set<string>([
  'string', 'number', 'boolean', 'select', 'multiselect', 'textarea',
  'date', 'file', 'input', 'toggle', 'slider', 'color', 'radio', 'checkbox',
])

function num(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined
}

function normalizeOptions(
  raw: unknown,
): Array<{ label: string; value: string | number }> | undefined {
  if (!Array.isArray(raw) || raw.length === 0) return undefined
  const out: Array<{ label: string; value: string | number }> = []
  for (const item of raw) {
    if (typeof item === 'string' || typeof item === 'number') {
      out.push({ label: String(item), value: item })
      continue
    }
    if (item && typeof item === 'object') {
      const o = item as { label?: unknown; value?: unknown }
      if (typeof o.value === 'string' || typeof o.value === 'number') {
        out.push({ label: typeof o.label === 'string' ? o.label : String(o.value), value: o.value })
      }
    }
  }
  return out.length > 0 ? out : undefined
}

/** fields 声明 → UIInputFormField[]（无效条目过滤；词汇外 type 兜底 string） */
export function toFormFields(
  fields: EnvConfigFieldDef[] | undefined | null,
): UIInputFormField[] {
  if (!Array.isArray(fields)) return []
  const out: UIInputFormField[] = []
  for (const f of fields) {
    if (!f || typeof f.name !== 'string' || f.name === '') continue
    const type =
      typeof f.type === 'string' && FIELD_TYPES.has(f.type) ? f.type : 'string'
    out.push({
      name: f.name,
      type: type as UIInputFormField['type'],
      label: typeof f.label === 'string' && f.label !== '' ? f.label : f.name,
      description: typeof f.description === 'string' ? f.description : undefined,
      required: Boolean(f.required),
      default: f.default,
      options: normalizeOptions(f.options),
      min: num(f.min),
      max: num(f.max),
      step: num(f.step),
      placeholder: typeof f.placeholder === 'string' ? f.placeholder : undefined,
      datasourceUri:
        typeof f.datasourceUri === 'string' ? f.datasourceUri : undefined,
      validation: f.validation,
    })
  }
  return out
}

/** 按点号路径从配置树读值（缺失返回 undefined，让字段 default 接管） */
export function getNestedValue(
  obj: Record<string, unknown>,
  path: string,
): unknown {
  let cur: unknown = obj
  for (const seg of path.split('.')) {
    if (cur == null || typeof cur !== 'object') return undefined
    cur = (cur as Record<string, unknown>)[seg]
  }
  return cur
}

/** 配置树 → 表单初值（按字段路径抽值；键为字段名原文，含点号） */
export function buildInitialValues(
  config: Record<string, unknown>,
  fields: UIInputFormField[],
): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const f of fields) {
    const v = getNestedValue(config, f.name)
    if (v !== undefined) out[f.name] = v
  }
  return out
}

/** 表单值按字段路径写回配置树副本（未声明键原样保留） */
export function mergeFormValues(
  config: Record<string, unknown>,
  fields: UIInputFormField[],
  values: Record<string, unknown>,
): Record<string, unknown> {
  let next = structuredClone(config)
  for (const f of fields) {
    if (!(f.name in values)) continue
    const path = f.name.split('.')
    const [head, ...rest] = path
    if (rest.length === 0) {
      next = { ...next, [head]: values[f.name] }
      continue
    }
    next = setNested(next, path, values[f.name])
  }
  return next
}

function setNested(
  obj: Record<string, unknown>,
  path: string[],
  value: unknown,
): Record<string, unknown> {
  const [head, ...rest] = path
  const next = { ...obj }
  if (rest.length === 0) {
    next[head] = value
    return next
  }
  const child = (next[head] as Record<string, unknown>) || {}
  next[head] = setNested({ ...child }, rest, value)
  return next
}
