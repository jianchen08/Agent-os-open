/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * RjsfForm 测试 — 统一表单核心
 *
 * 分两层：
 * 1. 纯函数层：toRjsf（词汇表→JSON Schema/uiSchema 映射）、buildFormValues（初始值合成）、
 *    makeErrorTransformer（ajv→中文文案）
 * 2. 组件层：asyncSelect 数据源拉取与选项渲染、字段模式（无提交按钮）
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { RJSFValidationError } from '@rjsf/utils'
import type { UIInputFormField } from '@/types/schema'

vi.mock('@/services/api/client', () => ({
  default: { get: vi.fn() },
}))

import apiClient from '@/services/api/client'

import { RjsfForm, buildFormValues, makeErrorTransformer, toRjsf } from '../RjsfForm'

describe('toRjsf — 词汇表映射', () => {
  it('string/input → string，textarea/date 挂对应 widget', () => {
    const { schema, uiSchema } = toRjsf([
      { name: 'a', type: 'string', label: 'A' },
      { name: 'b', type: 'input', label: 'B' },
      { name: 'c', type: 'textarea', label: 'C' },
      { name: 'd', type: 'date', label: 'D' },
    ])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.a).toMatchObject({ type: 'string', title: 'A' })
    expect(props.b).toMatchObject({ type: 'string', title: 'B' })
    expect(props.c).toMatchObject({ type: 'string' })
    expect(uiSchema.c).toMatchObject({ 'ui:widget': 'textarea' })
    expect(props.d).toMatchObject({ type: 'string' })
    expect(uiSchema.d).toMatchObject({ 'ui:widget': 'date' })
  })

  it('select → string + oneOf（值字符串化）；radio/multiselect 保留原始值', () => {
    const { schema, uiSchema } = toRjsf([
      { name: 's', type: 'select', label: 'S', options: [{ label: '主控', value: 'main' }] },
      { name: 'm', type: 'multiselect', label: 'M', options: [{ label: '质量', value: 'quality' }] },
      { name: 'r', type: 'radio', label: 'R', options: [{ label: '高', value: 2 }] },
    ])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.s).toMatchObject({ type: 'string', oneOf: [{ const: 'main', title: '主控' }] })
    expect(props.m).toMatchObject({
      type: 'array',
      uniqueItems: true,
      items: { oneOf: [{ const: 'quality', title: '质量' }] },
    })
    expect(uiSchema.m).toMatchObject({ 'ui:widget': 'checkboxes' })
    expect(props.r.oneOf).toEqual([{ const: 2, title: '高' }])
    expect(uiSchema.r).toMatchObject({ 'ui:widget': 'radio' })
  })

  it('number/slider 数值约束：validation 优先，字段级 min/max/step 兜底', () => {
    const { schema, uiSchema } = toRjsf([
      { name: 'n', type: 'number', label: 'N', validation: { min: 1, max: 9 } },
      { name: 'sl', type: 'slider', label: 'SL', min: 0, max: 100, step: 5 },
    ])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.n).toMatchObject({ type: 'number', minimum: 1, maximum: 9 })
    expect(props.sl).toMatchObject({ type: 'number', minimum: 0, maximum: 100, multipleOf: 5 })
    expect(uiSchema.sl).toMatchObject({ 'ui:widget': 'range' })
  })

  it('boolean/toggle → switch；color/file → 自定义 widget', () => {
    const { schema, uiSchema } = toRjsf([
      { name: 'b', type: 'boolean', label: 'B' },
      { name: 't', type: 'toggle', label: 'T' },
      { name: 'c', type: 'color', label: 'C' },
      { name: 'f', type: 'file', label: 'F' },
    ])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.b).toMatchObject({ type: 'boolean' })
    expect(props.t).toMatchObject({ type: 'boolean' })
    expect(uiSchema.b).toMatchObject({ 'ui:widget': 'switch' })
    expect(uiSchema.t).toMatchObject({ 'ui:widget': 'switch' })
    expect(uiSchema.c).toMatchObject({ 'ui:widget': 'colorPicker' })
    expect(uiSchema.f).toMatchObject({ 'ui:widget': 'filePicker' })
  })

  it('required 聚合到 schema.required；pattern 进 schema', () => {
    const { schema } = toRjsf([
      { name: 'a', type: 'string', label: 'A', required: true },
      { name: 'b', type: 'string', label: 'B', validation: { pattern: '^\\d+$' } },
    ])
    expect(schema.required).toEqual(['a'])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.b).toMatchObject({ pattern: '^\\d+$' })
  })

  it('datasourceUri → asyncSelect，schema 去掉枚举约束但保留 type', () => {
    const { schema, uiSchema } = toRjsf([
      {
        name: 'm',
        type: 'select',
        label: 'M',
        datasourceUri: 'models/list',
        options: [{ label: 'X', value: 'x' }],
      },
    ])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.m).not.toHaveProperty('oneOf')
    expect(props.m).toMatchObject({ type: 'string' })
    expect(uiSchema.m).toMatchObject({ 'ui:widget': 'asyncSelect' })
    expect(uiSchema.m['ui:options']).toMatchObject({ datasourceUri: 'models/list', multiple: false })
  })

  it('未知 type 回退 string', () => {
    const { schema } = toRjsf([
      { name: 'x', type: 'unknown-type' as UIInputFormField['type'], label: 'X' },
    ])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.x).toMatchObject({ type: 'string' })
  })
})

describe('buildFormValues — 初始值合成', () => {
  it('initialValues > default > 类型空值', () => {
    const fields: UIInputFormField[] = [
      { name: 'a', type: 'string', label: 'A', default: 'def' },
      { name: 'b', type: 'multiselect', label: 'B' },
      { name: 'c', type: 'toggle', label: 'C' },
      { name: 'd', type: 'slider', label: 'D', min: 10 },
    ]
    expect(buildFormValues(fields, { a: 'init' })).toEqual({
      a: 'init',
      b: [],
      c: false,
      d: 10,
    })
    // 无初始值时 default 生效
    expect(buildFormValues(fields, undefined)).toMatchObject({ a: 'def' })
  })
})

describe('makeErrorTransformer — 中文文案', () => {
  const fields: UIInputFormField[] = [
    { name: 'config_id', type: 'string', label: '配置ID', required: true },
    { name: 'code', type: 'string', label: '代码', validation: { pattern: '^\\d+$', message: '代码须为数字' } },
    { name: 'n', type: 'number', label: '数值' },
  ]

  it('required/pattern/minimum/type 文案', () => {
    const transform = makeErrorTransformer(fields)
    const errors = [
      { name: 'required', property: '.', params: { missingProperty: 'config_id' }, message: "must have required property 'config_id'" },
      { name: 'pattern', property: '.code', message: 'must match pattern' },
      { name: 'minimum', property: '.n', params: { limit: 3 }, message: 'must be >= 3' },
      { name: 'type', property: '.n', params: { type: 'number' }, message: 'must be number' },
    ] as unknown as RJSFValidationError[]
    const out = transform(errors, {})
    expect(out.map((e) => e.message)).toEqual([
      '配置ID不能为空',
      '代码须为数字',
      '最小值为 3',
      '请输入有效的数字',
    ])
  })
})

describe('RjsfForm — 组件层', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(apiClient.get).mockResolvedValue({ data: { options: [] } })
  })

  it('asyncSelect 拉取数据源并渲染选项（下拉可选中）', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      data: { options: [{ label: '模型A', value: 'model-a' }] },
    })
    const { container } = render(
      <RjsfForm
        fields={[{ name: 'model', type: 'select', label: '模型', datasourceUri: '/api/v1/models' }]}
        onSubmit={vi.fn()}
      />,
    )

    await waitFor(() => {
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/models')
    })
    // 打开下拉，选项来自数据源
    const select = container.querySelector('.ant-select')
    expect(select).not.toBeNull()
    fireEvent.mouseDown(select!.firstElementChild!)
    expect(await screen.findByText('模型A')).toBeInTheDocument()
  })

  it('不传 onSubmit → 字段模式，不渲染提交按钮', () => {
    render(
      <RjsfForm fields={[{ name: 'a', type: 'string', label: 'A' }]} onChange={vi.fn()} />,
    )
    expect(screen.queryByRole('button', { name: /提交/ })).not.toBeInTheDocument()
  })

  it('空字段列表 → 占位提示', () => {
    render(<RjsfForm fields={[]} onSubmit={vi.fn()} />)
    expect(screen.getByText('暂无表单字段')).toBeInTheDocument()
  })
})
