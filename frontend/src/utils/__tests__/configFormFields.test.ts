/**
 * configFormFields 单元测试（widget 化 T1）
 *
 * 覆盖：fields → UIInputFormField 收敛（词汇白名单/无效过滤/选项归一）、
 * 点号路径初值抽取、提交值按路径写回且未声明键保留。
 */
import { describe, it, expect } from 'vitest'
import {
  toFormFields,
  getNestedValue,
  buildInitialValues,
  mergeFormValues,
} from '@/utils/configFormFields'

describe('toFormFields — 声明收敛', () => {
  it('词汇表内 type 原样保留，词汇外兜底 string', () => {
    const out = toFormFields([
      { name: 'a', type: 'select', label: 'A' },
      { name: 'b', type: 'toggle', label: 'B' },
      { name: 'c', type: 'unknown-vocab', label: 'C' },
      { name: 'd', label: 'D' },
    ])
    expect(out.map((f) => f.type)).toEqual(['select', 'toggle', 'string', 'string'])
  })

  it('无效条目丢弃（无 name / 空 name / 非 object）', () => {
    const out = toFormFields([
      { label: '无 name' },
      { name: '', label: '空 name' },
      null,
      undefined,
      { name: 'ok', label: 'OK' },
    ] as never)
    expect(out).toHaveLength(1)
    expect(out[0].name).toBe('ok')
  })

  it('缺 label 用 name 兜底；options 归一（裸字符串/对象缺 label）', () => {
    const out = toFormFields([
      { name: 'x' },
      {
        name: 'y',
        type: 'select',
        options: ['裸选项', { value: 2 }, { label: '对象', value: 'v' }],
      },
    ])
    expect(out[0].label).toBe('x')
    expect(out[1].options).toEqual([
      { label: '裸选项', value: '裸选项' },
      { label: '2', value: 2 },
      { label: '对象', value: 'v' },
    ])
  })

  it('数值参数与 validation 透传；非法 number 置 undefined', () => {
    const out = toFormFields([
      { name: 'n', type: 'slider', min: 0, max: 10, step: 2, default: 4 },
      { name: 'm', type: 'number', min: 'x' as never },
    ])
    expect(out[0]).toMatchObject({ min: 0, max: 10, step: 2, default: 4 })
    expect(out[1].min).toBeUndefined()
  })

  it('fields 为 null/undefined/非数组 → 空数组', () => {
    expect(toFormFields(undefined)).toEqual([])
    expect(toFormFields(null)).toEqual([])
    expect(toFormFields('x' as never)).toEqual([])
  })
})

describe('点号路径寻址', () => {
  const config = {
    defaults: { chat: 'glm-5.2', tiers: { large: 'glm-5.2' } },
    concurrency: { chat: 8 },
    other: true,
  }
  const fields = toFormFields([
    { name: 'defaults.chat', type: 'string', label: 'chat' },
    { name: 'concurrency.chat', type: 'number', label: '并发' },
  ])

  it('getNestedValue 按路径取值 / 缺失返回 undefined / 越界安全', () => {
    expect(getNestedValue(config, 'defaults.chat')).toBe('glm-5.2')
    expect(getNestedValue(config, 'defaults.tiers.large')).toBe('glm-5.2')
    expect(getNestedValue(config, 'defaults.missing')).toBeUndefined()
    expect(getNestedValue(config, 'defaults.chat.deeper')).toBeUndefined()
  })

  it('buildInitialValues 按字段路径抽初值', () => {
    expect(buildInitialValues(config, fields)).toEqual({
      'defaults.chat': 'glm-5.2',
      'concurrency.chat': 8,
    })
  })

  it('mergeFormValues 按路径写回，未声明键原样保留', () => {
    const next = mergeFormValues(config, fields, {
      'defaults.chat': 'deepseek-v4',
      'concurrency.chat': 16,
    })
    expect(next.defaults.chat).toBe('deepseek-v4')
    expect(next.concurrency.chat).toBe(16)
    expect(next.defaults.tiers.large).toBe('glm-5.2')
    expect(next.other).toBe(true)
    // 原树不被变异
    expect(config.defaults.chat).toBe('glm-5.2')
  })

  it('mergeFormValues 路径中段缺失时逐级建对象', () => {
    const next = mergeFormValues({}, fields, { 'defaults.chat': 'm' })
    expect(next).toEqual({ defaults: { chat: 'm' } })
  })
})
