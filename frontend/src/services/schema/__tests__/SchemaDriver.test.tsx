/**
 * SchemaDriver 测试（渲染引擎 = RjsfForm：react-jsonschema-form + antd 主题）
 *
 * 验证字段级 Schema 驱动表单的核心行为：
 * - AC-1: 渲染全部字段 label；required 字段带 antd 必填标记
 * - AC-2: datasourceUri 字段挂载时调 apiClient 拉取动态选项（绝对 URI 与 datasource 前缀 URI）
 * - AC-3: multiselect 渲染为复选框组；date 渲染为 DatePicker 输入
 * - AC-4: required 校验：空必填字段提交被拦截并显示中文错误
 * - AC-5: 提交回调收到表单值（含多选数组与数字）
 * - AC-6: initialValues 预填（编辑场景）
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type { UIInputFormField } from '@/types/schema'

// ── Mock API client ──
vi.mock('@/services/api/client', () => ({
  default: { get: vi.fn() },
}))

import apiClient from '@/services/api/client'

import { SchemaDriver } from '../SchemaDriver'

/**
 * 提交动作：模拟表单 submit 事件（等价于用户点击 type=submit 按钮在真实浏览器
 * 触发的提交；jsdom 的 click 激活路径不重放该事件，故直接派发 submit）
 */
const submitForm = () => fireEvent.submit(document.querySelector('form')!)

/** 模拟后端 GET /api/v1/agents/schema 的 fields（覆盖 string/textarea/number/select/multiselect/date/datasource） */
const FIELDS: UIInputFormField[] = [
  { name: 'config_id', type: 'string', label: '配置ID', required: true },
  { name: 'name', type: 'string', label: '名称', required: true },
  { name: 'description', type: 'textarea', label: '描述' },
  {
    name: 'agent_type',
    type: 'select',
    label: '类型',
    options: [
      { label: '主控', value: 'main' },
      { label: '原子', value: 'atomic' },
    ],
  },
  {
    name: 'tags',
    type: 'multiselect',
    label: '标签',
    options: [
      { label: '质量', value: 'quality' },
      { label: '审查', value: 'review' },
    ],
  },
  { name: 'publish_date', type: 'date', label: '发布日期' },
  { name: 'max_iterations', type: 'number', label: '最大迭代' },
  { name: 'model', type: 'select', label: '模型', datasourceUri: '/api/v1/models' },
]

describe('SchemaDriver', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // 默认 datasource 响应（空选项），各用例可覆盖
    vi.mocked(apiClient.get).mockResolvedValue({ data: { options: [] } })
  })

  it('AC-1: 渲染所有字段的 label，required 字段带必填标记', () => {
    render(<SchemaDriver fields={FIELDS} onSubmit={vi.fn()} />)

    for (const field of FIELDS) {
      expect(screen.getByText(new RegExp(field.label))).toBeInTheDocument()
    }
    // antd required 标记打在 label class 上（ant-form-item-required）
    const requiredLabel = screen.getByText('配置ID').closest('label')
    expect(requiredLabel?.className).toMatch(/required/)
    const optionalLabel = screen.getByText('描述').closest('label')
    expect(optionalLabel?.className).not.toMatch(/required/)
  })

  it('AC-2: datasourceUri 字段挂载时调 apiClient 拉取选项（绝对 URI）', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({
      data: {
        options: [
          { label: '模型A', value: 'model-a' },
          { label: '模型B', value: 'model-b' },
        ],
      },
    })

    render(<SchemaDriver fields={FIELDS} onSubmit={vi.fn()} />)

    await waitFor(() => {
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/models')
    })
  })

  it('AC-2b: datasourceUri 无前导斜杠时走 /api/v1/datasource/ 前缀', async () => {
    vi.mocked(apiClient.get).mockResolvedValue({ data: ['cat-a', 'cat-b'] })

    const fields: UIInputFormField[] = [
      { name: 'category', type: 'select', label: '分类', datasourceUri: 'categories/list' },
    ]
    render(<SchemaDriver fields={fields} onSubmit={vi.fn()} />)

    await waitFor(() => {
      expect(apiClient.get).toHaveBeenCalledWith('/api/v1/datasource/categories/list')
    })
  })

  it('AC-3: multiselect 渲染复选框组，date 渲染日期输入', () => {
    render(<SchemaDriver fields={FIELDS} onSubmit={vi.fn()} />)

    // multiselect → checkbox 组
    expect(screen.getByLabelText('质量')).toHaveProperty('type', 'checkbox')
    expect(screen.getByLabelText('审查')).toHaveProperty('type', 'checkbox')

    // date → antd DatePicker（输入框存在）
    expect(screen.getByLabelText('发布日期')).toBeInTheDocument()
  })

  it('AC-4: required 校验拦截空必填字段并显示中文错误', () => {
    const onSubmit = vi.fn()
    render(<SchemaDriver fields={FIELDS} onSubmit={onSubmit} />)

    submitForm()

    expect(screen.getByText('配置ID不能为空')).toBeInTheDocument()
    expect(screen.getByText('名称不能为空')).toBeInTheDocument()
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it('AC-5: 提交回调收到表单值（含多选数组与数字）', () => {
    const onSubmit = vi.fn()
    render(<SchemaDriver fields={FIELDS} onSubmit={onSubmit} />)

    fireEvent.change(screen.getByLabelText('配置ID'), {
      target: { value: 'code_reviewer_agent' },
    })
    fireEvent.change(screen.getByLabelText('名称'), { target: { value: '代码审查专家' } })
    fireEvent.click(screen.getByLabelText('质量'))
    fireEvent.change(screen.getByLabelText('最大迭代'), { target: { value: '30' } })

    submitForm()

    expect(onSubmit).toHaveBeenCalledTimes(1)
    const values = onSubmit.mock.calls[0][0] as Record<string, unknown>
    expect(values.config_id).toBe('code_reviewer_agent')
    expect(values.name).toBe('代码审查专家')
    expect(values.tags).toEqual(['quality'])
    expect(values.max_iterations).toBe(30)
  })

  it('AC-6: initialValues 预填表单（编辑场景）', () => {
    render(
      <SchemaDriver
        fields={FIELDS}
        initialValues={{ config_id: 'code_reviewer_agent', name: '代码审查专家', tags: ['review'] }}
        onSubmit={vi.fn()}
      />,
    )

    expect(screen.getByLabelText('配置ID')).toHaveValue('code_reviewer_agent')
    expect(screen.getByLabelText('名称')).toHaveValue('代码审查专家')
    expect(screen.getByLabelText('审查')).toBeChecked()
  })
})
