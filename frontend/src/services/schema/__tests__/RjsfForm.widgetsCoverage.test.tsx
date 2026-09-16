// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * RjsfForm 自定义 widget/校验分支补测
 *
 * 覆盖既有 RjsfForm.test.tsx / FormWidget.* 未触达的分支：
 * - radio 无 options 声明 → 回退 string（避免 RJSF 无 type 不渲染）
 * - datasourceUri + 多选类型 → schema 保持 array/uniqueItems（去枚举但保留数组语义）
 * - maximum 校验的 ajv → 中文文案（「最大值为 n」）
 * - colorPicker：十六进制回显（初值透传）、缺值兜底 #000000、输入事件回写表单值
 * - filePicker：表单值存文件名（e.target.files[0].name）、未选文件回写空串
 *
 * 断行为：用户可见的渲染产物与 onChange 载荷（可观察输入→输出），不断言内部实现。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import apiClient from '@/services/api/client'
import { RjsfForm, makeErrorTransformer, toRjsf } from '../RjsfForm'
import type { UIInputFormField } from '@/types/schema'
import type { RJSFValidationError } from '@rjsf/utils'

vi.mock('@/services/api/client', () => ({
  default: { get: vi.fn() },
}))

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(apiClient.get).mockResolvedValue({ data: { options: [] } })
})

describe('toRjsf — 残余类型分派分支', () => {
  it('radio 无 options → type 回退 string（有 type 才会渲染 widget）', () => {
    const { schema, uiSchema } = toRjsf([
      { name: 'r', type: 'radio', label: '无选项单选' },
    ])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.r).toMatchObject({ type: 'string' })
    expect(props.r).not.toHaveProperty('oneOf')
    expect(uiSchema.r).toMatchObject({ 'ui:widget': 'radio' })
  })

  it('radio 有选项 → 按首个选项值类型推断 type（数字选项 → number）', () => {
    const { schema } = toRjsf([
      { name: 'r', type: 'radio', label: '档位', options: [{ label: '高', value: 2 }] },
    ])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.r).toMatchObject({ type: 'number', oneOf: [{ const: 2, title: '高' }] })
  })

  it.each([
    { type: 'multiselect' as const, label: '多选' },
    { type: 'checkbox' as const, label: '复选' },
  ])(
    'datasourceUri + $type → 去枚举但保持 array/uniqueItems，widget 走 asyncSelect 且 multiple=true',
    ({ type, label }) => {
      const { schema, uiSchema } = toRjsf([
        {
          name: 'ds',
          type,
          label,
          datasourceUri: 'agents/list',
          options: [{ label: '甲', value: 'a' }],
        },
      ])
      const props = schema.properties as Record<string, Record<string, unknown>>
      expect(props.ds).toMatchObject({ type: 'array', uniqueItems: true })
      expect(props.ds).not.toHaveProperty('oneOf')
      expect(props.ds).not.toHaveProperty('items')
      expect(uiSchema.ds).toMatchObject({ 'ui:widget': 'asyncSelect' })
      expect(uiSchema.ds['ui:options']).toMatchObject({
        datasourceUri: 'agents/list',
        multiple: true,
        fallbackOptions: [{ label: '甲', value: 'a' }],
      })
    },
  )

  it('datasourceUri + 标量类型 → type 归一为 string，multiple=false', () => {
    const { schema, uiSchema } = toRjsf([
      { name: 'ds', type: 'select', label: '单选', datasourceUri: '/ext/models' },
    ])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.ds).toMatchObject({ type: 'string' })
    expect(uiSchema.ds['ui:options']).toMatchObject({ multiple: false })
  })
})

describe('makeErrorTransformer — maximum 分支', () => {
  const fields: UIInputFormField[] = [
    { name: 'n', type: 'number', label: '阈值', validation: { max: 100 } },
  ]

  it('maximum → 「最大值为 n」（取 ajv params.limit）', () => {
    const transform = makeErrorTransformer(fields)
    const errors = [
      {
        name: 'maximum',
        property: '.n',
        params: { limit: 100 },
        message: 'must be <= 100',
      },
    ] as unknown as RJSFValidationError[]

    const out = transform(errors, {})

    expect(out[0].message).toBe('最大值为 100')
  })

  it.each([
    { limit: 100, expected: '最大值为 100' },
    { limit: 0.5, expected: '最大值为 0.5' },
  ])('maximum limit=$limit 文案随值变化（非硬编码）', ({ limit, expected }) => {
    const transform = makeErrorTransformer(fields)
    const errors = [
      { name: 'maximum', property: '.n', params: { limit }, message: 'x' },
    ] as unknown as RJSFValidationError[]

    expect(transform(errors, {})[0].message).toBe(expected)
  })
})

describe('RjsfForm — colorPicker widget', () => {
  const colorField: UIInputFormField[] = [{ name: 'c', type: 'color', label: '主题色' }]

  it('缺值兜底 #000000 且回显十六进制', () => {
    const { container } = render(<RjsfForm fields={colorField} onChange={vi.fn()} />)
    const input = container.querySelector('input[type="color"]') as HTMLInputElement

    expect(input).not.toBeNull()
    expect(input.value).toBe('#000000')
    expect(input.id).toBe('root_c')
    expect(container.textContent).toContain('#000000')
  })

  it('初值透传：取色器与回显文本同步为初值', () => {
    const { container } = render(
      <RjsfForm fields={colorField} initialValues={{ c: '#123abc' }} onChange={vi.fn()} />,
    )
    const input = container.querySelector('input[type="color"]') as HTMLInputElement

    expect(input.value).toBe('#123abc')
    expect(container.textContent).toContain('#123abc')
  })

  it('取色输入 → 表单值回写为十六进制字符串', () => {
    const onChange = vi.fn()
    const { container } = render(<RjsfForm fields={colorField} onChange={onChange} />)
    const input = container.querySelector('input[type="color"]') as HTMLInputElement

    fireEvent.change(input, { target: { value: '#ff8800' } })

    expect(onChange).toHaveBeenCalled()
    expect(onChange.mock.calls.at(-1)?.[0]).toMatchObject({ c: '#ff8800' })
    // 回显同步（受控：值来自表单 state）
    expect(input.value).toBe('#ff8800')
    expect(container.textContent).toContain('#ff8800')
  })

  it('readonly/disabled → 取色器不可编辑（只读展示语义）', () => {
    const { container } = render(<RjsfForm fields={colorField} disabled onChange={vi.fn()} />)
    const input = container.querySelector('input[type="color"]') as HTMLInputElement

    expect(input.disabled).toBe(true)
  })
})

describe('RjsfForm — asyncSelect 值回写', () => {
  const dsField: UIInputFormField[] = [
    { name: 'model', type: 'select', label: '模型', datasourceUri: '/api/v1/models' },
  ]

  it('单选下拉：选中项回写为字符串（数值选项也字符串化）', async () => {
    const onChange = vi.fn()
    vi.mocked(apiClient.get).mockResolvedValue({
      data: { options: [{ label: '模型B', value: 42 }] },
    })
    const { container } = render(<RjsfForm fields={dsField} onChange={onChange} />)

    await waitFor(() => expect(apiClient.get).toHaveBeenCalledWith('/api/v1/models'))
    const select = container.querySelector('.ant-select')
    expect(select).not.toBeNull()
    fireEvent.mouseDown(select!.firstElementChild!)
    fireEvent.click(await screen.findByText('模型B'))

    expect(onChange.mock.calls.at(-1)?.[0]).toMatchObject({ model: '42' })
  })

  it.each([
    { label: '模型A', value: 'model-a' },
    { label: '模型C', value: 7 },
  ])('选中 $label → 值统一字符串化（$value）', async ({ label, value }) => {
    const onChange = vi.fn()
    vi.mocked(apiClient.get).mockResolvedValue({ data: { options: [{ label, value }] } })
    const { container } = render(<RjsfForm fields={dsField} onChange={onChange} />)

    await waitFor(() => expect(apiClient.get).toHaveBeenCalled())
    fireEvent.mouseDown(container.querySelector('.ant-select')!.firstElementChild!)
    fireEvent.click(await screen.findByText(label))

    expect(onChange.mock.calls.at(-1)?.[0]).toEqual({ model: String(value) })
  })
})

describe('RjsfForm — filePicker widget', () => {
  const fileField: UIInputFormField[] = [{ name: 'f', type: 'file', label: '附件' }]

  it('选择文件 → 表单值存文件名（不存路径/内容）', () => {
    const onChange = vi.fn()
    const { container } = render(<RjsfForm fields={fileField} onChange={onChange} />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement

    fireEvent.change(input, {
      target: { files: [new File(['payload'], 'report.pdf', { type: 'application/pdf' })] },
    })

    expect(onChange.mock.calls.at(-1)?.[0]).toMatchObject({ f: 'report.pdf' })
  })

  it('取消选择（files 为空）→ 回写空串（清空旧文件名）', () => {
    const onChange = vi.fn()
    const { container } = render(
      <RjsfForm fields={fileField} initialValues={{ f: '旧文件.txt' }} onChange={onChange} />,
    )
    const input = container.querySelector('input[type="file"]') as HTMLInputElement
    expect(input.getAttribute('data-value')).toBe('旧文件.txt')

    fireEvent.change(input, { target: { files: [] } })

    expect(onChange.mock.calls.at(-1)?.[0]).toMatchObject({ f: '' })
  })

  it.each([
    { name: 'a.csv' },
    { name: '中文报表.xlsx' },
  ])('文件名 $name 原样入库（不作路径/扩展名加工）', ({ name }) => {
    const onChange = vi.fn()
    const { container } = render(<RjsfForm fields={fileField} onChange={onChange} />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement

    fireEvent.change(input, { target: { files: [new File(['x'], name)] } })

    expect(onChange.mock.calls.at(-1)?.[0]).toMatchObject({ f: name })
  })

  it('readonly/disabled → 文件选择不可用', () => {
    const { container } = render(<RjsfForm fields={fileField} disabled onChange={vi.fn()} />)
    const input = container.querySelector('input[type="file"]') as HTMLInputElement

    expect(input.disabled).toBe(true)
    expect(screen.getByText('附件')).toBeInTheDocument()
  })
})
