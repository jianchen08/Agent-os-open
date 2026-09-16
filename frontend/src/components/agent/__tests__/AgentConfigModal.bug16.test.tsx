/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * BUG-16 回归 — 现象B：Agent 编辑模态 array 无 items 字段渲染 + 保存链路
 *
 * AgentConfigModal（FormWidget modal 壳 + fieldsUri/dataUri datasource 模式）：
 * agent_manager schema 端点返回的 tool_ids/tags 是 multiselect 且无 options →
 * toRjsf 曾出 {type:'array'} 无 items → RJSF UnsupportedField 兜底渲染成不可
 * 编辑的裸 JSON 文本。
 *
 * 回归要求：①tags/tool_ids 渲染非 UnsupportedField（现值逐项可编辑）；
 * ②改超时秒提交 → PUT dataUri 出网（yaml 体含新值 + 数组字段值原样带回）。
 *
 * 注：勿用 getByRole('group', {name}) 断言本表单——dom-accessibility-api 在
 * 该 antd+RJSF 嵌套 DOM 上算可访问名会让 vitest worker 直接崩溃（栈溢出），
 * 用 getByDisplayValue 等直查断言。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { AgentConfigModal } from '@/components/agent/AgentConfigModal'

// FormWidget 消费 useSessionsQuery（权限档显示默认），本文件静态 mock 空列表
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: () => ({ data: [] }),
}))

const apiGet = vi.fn()
const apiRequest = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    (...args: unknown[]) => apiRequest(...args),
    { get: (...args: unknown[]) => apiGet(...args) },
  ),
}))

/** agent_manager /agents/schema 返回的字段声明（tool_ids/tags 为 multiselect 无 options） */
const AGENT_FIELDS = [
  { name: 'config_id', type: 'string', label: '配置ID', required: true },
  { name: 'name', type: 'string', label: '名称', required: true },
  { name: 'display_name', type: 'string', label: '显示名称' },
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
  { name: 'model_tier', type: 'string', label: '模型档位' },
  { name: 'system_prompt', type: 'textarea', label: '系统提示词' },
  { name: 'tool_ids', type: 'multiselect', label: '工具' },
  { name: 'max_iterations', type: 'number', label: '最大迭代' },
  { name: 'timeout_seconds', type: 'number', label: '超时秒' },
  { name: 'tags', type: 'multiselect', label: '标签' },
]

/** code_writer agent yaml（表单字段键 + 未声明键：BUG-18 往返保真断言用） */
const CODE_WRITER_YAML = `config_id: code_writer_agent
name: code_writer
display_name: 代码编写专家
description: 代码编写专家
agent_type: atomic
model_tier: medium
tool_ids:
- file_read
- file_write
max_iterations: 500
timeout_seconds: 2400
tags:
- code
- generation
category: specialized
version: 1.0.0
is_active: true
static_vars:
  language: python
metadata:
  owner: platform
`

const CONFIG_URI = '/ext/agent_manager/agents/code_writer/config'
const SCHEMA_URI = '/ext/agent_manager/agents/schema'

beforeEach(() => {
  apiGet.mockReset()
  apiRequest.mockReset()
  apiGet.mockImplementation((url: string) => {
    if (url === SCHEMA_URI) return Promise.resolve({ data: { fields: AGENT_FIELDS } })
    // agent_manager GET config 信封：{config_id, yaml, etag}（etag=磁盘原文指纹）
    if (url === CONFIG_URI)
      return Promise.resolve({ data: { config_id: 'code_writer', yaml: CODE_WRITER_YAML, etag: 'etag-1' } })
    return Promise.reject(new Error(`unexpected GET ${url}`))
  })
  apiRequest.mockResolvedValue({ data: {} })
})

describe('BUG-16 现象B — Agent 编辑模态', () => {
  it('tags/tool_ids（multiselect 无 options）渲染为可编辑数组字段而非 UnsupportedField', async () => {
    render(
      <AgentConfigModal
        agent={{ id: 'code_writer', name: 'code_writer' }}
        isOpen
        onClose={vi.fn()}
      />,
    )
    await screen.findByLabelText('超时秒')
    // UnsupportedField 兜底文案不得出现（修复前 tags/tool_ids 渲染成裸 JSON 文本）
    expect(screen.queryAllByText(/Unsupported field schema/i)).toHaveLength(0)
    // 现有值回显为逐项可编辑输入（items:{type:'string'} 默认数组字段，可增删改）
    expect(screen.getByDisplayValue('code')).toBeInTheDocument()
    expect(screen.getByDisplayValue('generation')).toBeInTheDocument()
    expect(screen.getByDisplayValue('file_read')).toBeInTheDocument()
    expect(screen.getByDisplayValue('file_write')).toBeInTheDocument()
  })

  it('改超时秒后保存：PUT dataUri 出网，yaml 体带新值且数组字段值原样带回', async () => {
    render(
      <AgentConfigModal
        agent={{ id: 'code_writer', name: 'code_writer' }}
        isOpen
        onClose={vi.fn()}
      />,
    )
    const timeout = await screen.findByLabelText('超时秒')
    fireEvent.change(timeout, { target: { value: '2401' } })
    fireEvent.submit(document.querySelector('form')!)
    await waitFor(() => expect(apiRequest).toHaveBeenCalled())
    const [config] = apiRequest.mock.calls[0]
    expect(config.method).toBe('PUT')
    expect(config.url).toBe(CONFIG_URI)
    expect(config.data.yaml).toContain('timeout_seconds: 2401')
    // 数组字段值（tool_ids/tags）进入保存 payload
    expect(config.data.yaml).toContain('file_read')
    expect(config.data.yaml).toContain('code')
    // 乐观锁：GET 捕获的 etag 以 body if_match 回传（agent_manager 写面缺它恒 409）
    expect(config.data.if_match).toBe('etag-1')
    // BUG-18：保存往返后未声明键原样保留（标量/布尔/嵌套对象三形态），
    // PUT 不得是只剩表单字段值的剥键序列化
    expect(config.data.yaml).toContain('category: specialized')
    expect(config.data.yaml).toContain('is_active: true')
    expect(config.data.yaml).toContain('language: python')
    expect(config.data.yaml).toContain('owner: platform')
    // 保存反馈可见（form-widget-status）
    expect(await screen.findByTestId('form-widget-status')).toHaveTextContent('已保存')
  })

  it('PUT 409 冲突：显式提示「配置已被他人修改」，不静默不误报成功', async () => {
    apiRequest.mockRejectedValueOnce({ response: { status: 409 } })
    render(
      <AgentConfigModal
        agent={{ id: 'code_writer', name: 'code_writer' }}
        isOpen
        onClose={vi.fn()}
      />,
    )
    const timeout = await screen.findByLabelText('超时秒')
    fireEvent.submit(document.querySelector('form')!)
    await waitFor(() => expect(apiRequest).toHaveBeenCalled())
    // 409 走显式冲突文案（对齐 PluginConfigEditor isPluginConfigConflict 语义）
    expect(await screen.findByTestId('form-widget-status')).toHaveTextContent('配置已被他人修改')
    // 冲突不得显示成功
    expect(screen.queryByText('已保存')).not.toBeInTheDocument()
  })
})
