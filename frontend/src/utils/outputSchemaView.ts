/**
 * 工具 output_schema 前端消费（widget 化 T4）。
 *
 * output_schema 是工具输出契约（JSON Schema），内核侧 tool_core 已按
 * fail-closed 校验（output_validate.rs：type/enum/required/items 子集）。
 * 本模块是前端结构化视图消费：
 * - schema.properties → UIInputFormField[]（词汇收敛）→ 工具卡片内只读
 *   FormWidget 渲染（经 contentType:'form' 的 formFields 形状路由）；
 * - validateOutputSubset：tool_core 校验语义的前端镜像（违规标警展示，
 *   权威校验仍在内核 fail-closed）。
 *
 * 优先级：插件显式声明（render/chat_card）永远优先——本视图只兜
 * 「有契约、无声明」的工具（见 enhanceActivityWithToolConfig）。
 */
import { safeParseResult } from '@/utils/toolCardRegistry'
import type { ActivityDetailBlock } from '@/types/activity'
import type { UIInputFormField } from '@/types/schema'

// ── 契约注册表：toolName → output_schema（从 /api/v1/schema 的 tools[] 装载）──
const outputSchemas = new Map<string, Record<string, unknown>>()

/** 从 schema.tools[] 装载 output_schema 注册表（幂等：先清空再装） */
export function loadOutputSchemas(
  tools: Array<{ name?: string; output_schema?: Record<string, unknown> }>,
): void {
  outputSchemas.clear()
  for (const t of tools) {
    if (t.name && t.output_schema && typeof t.output_schema === 'object') {
      outputSchemas.set(t.name, t.output_schema)
    }
  }
}

/** 按 toolName 查 output_schema */
export function getOutputSchema(toolName: string): Record<string, unknown> | undefined {
  return outputSchemas.get(toolName)
}

// ── schema → 表单字段词汇 ──

function schemaTypeToFieldType(prop: Record<string, unknown>): UIInputFormField['type'] {
  const enumValues = prop.enum
  if (Array.isArray(enumValues)) {
    return Array.isArray(prop.type) && (prop.type as string[]).includes('array')
      ? 'multiselect'
      : 'select'
  }
  const t = Array.isArray(prop.type) ? (prop.type as string[])[0] : prop.type
  switch (t) {
    case 'integer':
    case 'number':
      return 'number'
    case 'boolean':
      return 'toggle'
    case 'array': {
      const items = prop.items as Record<string, unknown> | undefined
      return items && Array.isArray(items.enum) ? 'multiselect' : 'string'
    }
    default:
      return 'string'
  }
}

/**
 * output_schema（object/properties 形态）→ UIInputFormField[]。
 *
 * 非 object schema（true/null/数组根）返回空（无字段可结构化展示）。
 */
export function outputSchemaToFormFields(schema: Record<string, unknown>): UIInputFormField[] {
  const props = schema.properties
  if (!props || typeof props !== 'object' || Array.isArray(props)) return []
  const required = new Set(
    Array.isArray(schema.required) ? (schema.required as unknown[]).map(String) : [],
  )
  const out: UIInputFormField[] = []
  for (const [name, raw] of Object.entries(props as Record<string, unknown>)) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) continue
    const prop = raw as Record<string, unknown>
    const type = schemaTypeToFieldType(prop)
    const options =
      type === 'select' || type === 'multiselect'
        ? (prop.enum as unknown[]).map((v) => ({ label: String(v), value: v as string | number }))
        : undefined
    out.push({
      name,
      type,
      label: typeof prop.title === 'string' ? prop.title : name,
      description: typeof prop.description === 'string' ? prop.description : undefined,
      required: required.has(name),
      default: prop.default,
      options,
    })
  }
  return out
}

/** 只读展示的值收敛（数组/对象落 string 字段时 JSON 序列化，避免控件类型错配） */
export function coerceDisplayValues(
  fields: UIInputFormField[],
  data: Record<string, unknown>,
): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const f of fields) {
    const v = data[f.name]
    if (v === undefined) continue
    if (f.type === 'string') {
      out[f.name] = typeof v === 'object' && v !== null ? JSON.stringify(v, null, 2) : v
    } else if (f.type === 'number') {
      out[f.name] = typeof v === 'number' ? v : Number.isFinite(Number(v)) ? Number(v) : v
    } else {
      out[f.name] = v
    }
  }
  return out
}

// ── tool_core 校验语义前端镜像（type/enum/required/items 子集，宽松忽略组合关键字）──

function typeMatches(expected: unknown, value: unknown): boolean {
  const check = (t: string): boolean => {
    switch (t) {
      case 'object':
        return typeof value === 'object' && value !== null && !Array.isArray(value)
      case 'array':
        return Array.isArray(value)
      case 'string':
        return typeof value === 'string'
      case 'boolean':
        return typeof value === 'boolean'
      case 'number':
        return typeof value === 'number' && Number.isFinite(value)
      case 'integer':
        // 整值浮点（1.0）宽容，对齐 tool_core
        return typeof value === 'number' && Number.isInteger(value)
      case 'null':
        return value === null
      default:
        return true
    }
  }
  if (Array.isArray(expected)) return expected.some(check)
  if (typeof expected === 'string') return check(expected)
  return true
}

/**
 * 校验数据是否符合 output_schema 子集（tool_core output_validate.rs 语义镜像）。
 *
 * 返回违规消息列表（空 = 合规）。仅覆盖常用子集：type / enum / required /
 * items 递归；additionalProperties/$ref/组合关键字忽略（宽松，同内核）。
 */
export function validateOutputSubset(
  schema: Record<string, unknown>,
  data: unknown,
  path = '',
): string[] {
  const errors: string[] = []
  if (!schema || typeof schema !== 'object') return errors

  if (schema.type === 'object' || schema.properties) {
    if (typeof data !== 'object' || data === null || Array.isArray(data)) {
      if (schema.type === 'object') {
        errors.push(`${path || '根'}: expected type object, got ${Array.isArray(data) ? 'array' : typeof data}`)
      }
      return errors
    }
    const obj = data as Record<string, unknown>
    for (const req of Array.isArray(schema.required) ? (schema.required as unknown[]) : []) {
      const reqKey = String(req)
      if (!(reqKey in obj) || obj[reqKey] === undefined) {
        errors.push(`${path || '根'}: missing required field \`${reqKey}\``)
      }
    }
    const props = (schema.properties ?? {}) as Record<string, Record<string, unknown>>
    for (const [key, prop] of Object.entries(props)) {
      if (obj[key] === undefined) continue
      errors.push(...validateOutputSubset(prop, obj[key], `${path}.${key}`))
    }
    return errors
  }

  if (Array.isArray(schema.type) || typeof schema.type === 'string') {
    if (!typeMatches(schema.type, data)) {
      errors.push(
        `${path}: expected type ${JSON.stringify(schema.type)}, got ${
          data === null ? 'null' : Array.isArray(data) ? 'array' : typeof data
        }`,
      )
    }
  }

  if (Array.isArray(schema.enum) && !schema.enum.some((e) => e === data)) {
    errors.push(`${path}: value ${JSON.stringify(data)} not in enum [${schema.enum.map(String).join(', ')}]`)
  }

  if (Array.isArray(data) && schema.items && typeof schema.items === 'object') {
    const items = schema.items as Record<string, unknown>
    data.forEach((item, i) => {
      errors.push(...validateOutputSubset(items, item, `${path}[${i}]`))
    })
  }

  return errors
}

// ── 工具调用 → 结构化视图块 ──

export interface OutputSchemaViewResult {
  block: ActivityDetailBlock
  /** 契约违规消息（fail-closed 前端镜像；空 = 合规） */
  violations: string[]
}

/**
 * 为带 output_schema 的工具调用构造结构化输出块（只读）。
 *
 * 数据形态：resultData（结构化）优先，字符串 result 经 safeParseResult 解析；
 * 解析不出 object → null（非结构化输出不适用本视图）。
 */
export function buildOutputSchemaView(
  schema: Record<string, unknown>,
  result: unknown,
  resultData?: unknown,
): OutputSchemaViewResult | null {
  const fields = outputSchemaToFormFields(schema)
  if (fields.length === 0) return null
  const parsed =
    resultData && typeof resultData === 'object'
      ? (resultData as Record<string, unknown>)
      : typeof result === 'string'
        ? safeParseResult(result)
        : result && typeof result === 'object'
          ? (result as Record<string, unknown>)
          : null
  if (!parsed) return null
  return {
    block: {
      id: 'output_schema_view',
      label: '结构化输出',
      contentType: 'form',
      content: {
        formFields: fields,
        values: coerceDisplayValues(fields, parsed),
        readOnly: true,
      },
    },
    violations: validateOutputSubset(schema, parsed),
  }
}
