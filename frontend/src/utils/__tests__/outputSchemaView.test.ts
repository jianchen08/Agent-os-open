/**
 * outputSchemaView 单元测试（widget 化 T4）+ enhance 链接线测试
 */
import { beforeEach, describe, expect, it } from 'vitest'
import {
  buildOutputSchemaView,
  getOutputSchema,
  loadOutputSchemas,
  outputSchemaToFormFields,
  coerceDisplayValues,
  validateOutputSubset,
} from '@/utils/outputSchemaView'
import { enhanceActivityWithToolConfig } from '@/utils/toolCardRegistry'
import { addChatCardDeclaration, clearChatCardDeclarations } from '@/utils/chatCardInterpreter'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

/** bash_execute 同款契约（真实样例） */
const bashSchema = {
  type: 'object',
  properties: {
    status: { type: 'string', enum: ['completed', 'running', 'terminated'] },
    pid: { type: 'integer' },
    exit_code: { type: 'integer' },
    output: { type: 'string' },
    summary: { type: 'array', items: { type: 'string' } },
    elapsed: { type: 'number' },
  },
  required: ['status'],
}

beforeEach(() => {
  clearChatCardDeclarations()
})

describe('outputSchemaToFormFields — 契约 → 字段词汇', () => {
  it('enum→select、integer/number→number、array→string、required 标记', () => {
    const fields = outputSchemaToFormFields(bashSchema)
    const byName = Object.fromEntries(fields.map((f) => [f.name, f]))
    expect(byName.status.type).toBe('select')
    expect(byName.status.options).toHaveLength(3)
    expect(byName.status.required).toBe(true)
    expect(byName.pid.type).toBe('number')
    expect(byName.summary.type).toBe('string') // array 无 items.enum → string
    expect(byName.output.type).toBe('string')
    expect(byName.elapsed.type).toBe('number')
  })

  it('title/description/default 透传；非 object schema → 空数组', () => {
    const fields = outputSchemaToFormFields({
      type: 'object',
      properties: {
        verbose: { type: 'boolean', title: '详细', description: '是否输出详情', default: false },
      },
    })
    expect(fields[0]).toMatchObject({ label: '详细', description: '是否输出详情', default: false, type: 'toggle' })
    expect(outputSchemaToFormFields({ type: 'string' })).toEqual([])
    expect(outputSchemaToFormFields({})).toEqual([])
  })

  it('coerceDisplayValues：数组/对象落 string 字段 JSON 化，数值字符串转数字', () => {
    const fields = outputSchemaToFormFields(bashSchema)
    const out = coerceDisplayValues(fields, {
      summary: ['a', 'b'],
      elapsed: '1.5',
      status: 'completed',
    })
    expect(out.summary).toBe(JSON.stringify(['a', 'b'], null, 2))
    expect(out.elapsed).toBe(1.5)
    expect(out.status).toBe('completed')
  })
})

describe('validateOutputSubset — tool_core 语义镜像', () => {
  it('合规数据无违规', () => {
    expect(
      validateOutputSubset(bashSchema, { status: 'completed', exit_code: 0, summary: ['ok'] }),
    ).toEqual([])
  })

  it('required 缺失 / enum 越界 / 类型错配 / items 递归', () => {
    const errs = validateOutputSubset(bashSchema, {
      status: 'weird',
      pid: '123',
      summary: ['ok', 42],
    })
    const joined = errs.join('\n')
    expect(joined).not.toContain('missing required') // status 有值不缺 required
    expect(joined).toContain('not in enum')
    expect(joined).toContain('expected type')
    expect(joined).toContain('[1]')
  })

  it('required 缺失报 missing required field', () => {
    const errs = validateOutputSubset(bashSchema, { pid: 1 })
    expect(errs.join('\n')).toContain('missing required field `status`')
  })

  it('整值浮点宽容（integer 接受 1.0）——对齐 tool_core', () => {
    expect(validateOutputSubset(bashSchema, { status: 'completed', pid: 1.0 })).toEqual([])
  })
})

describe('buildOutputSchemaView — 调用 → 结构化块', () => {
  it('resultData 优先；产出只读 form 块（formFields+values+readOnly）', () => {
    const view = buildOutputSchemaView(
      bashSchema,
      'ignored-string-result',
      { status: 'completed', output: 'done', elapsed: 2 },
    )
    expect(view).not.toBeNull()
    expect(view!.block.contentType).toBe('form')
    expect(view!.block.content).toMatchObject({ readOnly: true })
    expect(view!.violations).toEqual([])
    expect((view!.block.content as Record<string, unknown>).values).toMatchObject({
      status: 'completed',
    })
  })

  it('字符串 result 经 safeParseResult 解析；解析不出 object → null', () => {
    const view = buildOutputSchemaView(bashSchema, '{"status": "running"}')
    expect(view).not.toBeNull()
    expect(buildOutputSchemaView(bashSchema, 'plain text output')).toBeNull()
  })

  it('契约违规产出违规消息列表', () => {
    const view = buildOutputSchemaView(bashSchema, { status: 'bogus' })
    expect(view!.violations.join('\n')).toContain('not in enum')
  })
})

describe('enhance 链接线（优先级）', () => {
  const baseActivity: ActivityData = {
    type: 'tool_call',
    id: 't1',
    title: 'contract_tool',
    toolName: 'contract_tool',
    status: 'completed',
  }
  const toolCall = {
    tool_name: 'contract_tool',
    tool_args: {},
    resultData: { status: 'completed', output: 'ok' },
  } as unknown as MessageToolCall

  it('带 output_schema 且无声明 → 结构化视图块 + 违规写入 error 展示', () => {
    loadOutputSchemas([{ name: 'contract_tool', output_schema: bashSchema }])
    const out = enhanceActivityWithToolConfig(baseActivity, toolCall)
    const block = out.details?.find((d) => d.id === 'output_schema_view')
    expect(block).toBeDefined()
    expect(out.error).toBeUndefined()
  })

  it('契约违规 → activity.error 携带前端镜像违规消息', () => {
    loadOutputSchemas([{ name: 'contract_tool', output_schema: bashSchema }])
    const bad = { ...toolCall, resultData: { status: 'bogus' } } as unknown as MessageToolCall
    const out = enhanceActivityWithToolConfig(baseActivity, bad)
    expect(out.error).toContain('output_schema')
    expect(out.error).toContain('not in enum')
  })

  it('chat_card 显式声明优先于 output_schema 视图', () => {
    loadOutputSchemas([{ name: 'contract_tool', output_schema: bashSchema }])
    addChatCardDeclaration('contract_tool', { title: '声明卡片', blocks: [{ type: 'text', source: 'result.output' }] })
    const out = enhanceActivityWithToolConfig(baseActivity, toolCall)
    expect(out.title).toBe('声明卡片')
    expect(out.details?.some((d) => d.id === 'output_schema_view')).toBe(false)
  })

  it('getOutputSchema 注册表装载/查询', () => {
    loadOutputSchemas([{ name: 'x', output_schema: bashSchema }, { name: 'no-schema' }])
    expect(getOutputSchema('x')).toBeDefined()
    expect(getOutputSchema('no-schema')).toBeUndefined()
  })
})
