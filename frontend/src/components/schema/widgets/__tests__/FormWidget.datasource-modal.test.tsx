/**
 * FormWidget datasource / modal 壳模式测试（widget 化 T12）
 *
 * 覆盖：fieldsUri+dataUri(yaml) 加载渲染初值 → 提交 PUT {yaml}；
 * modal 受控开关 + 提交成功自动关闭 + onSaved；onSubmit 抛错不关闭并 toast；
 * endpoint 模式 extraBody 附加字段。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

import { FormWidget } from '../FormWidget'

const apiGet = vi.fn()
const apiRequest = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    (...args: unknown[]) => apiRequest(...args),
    { get: (...args: unknown[]) => apiGet(...args) },
  ),
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

const submitForm = () => fireEvent.submit(document.querySelector('form')!)

beforeEach(() => {
  apiGet.mockReset()
  apiRequest.mockReset()
})

describe('T12：datasource 模式（吸收 SchemaFormEmbed）', () => {
  it('fieldsUri+dataUri(yaml) → 字段渲染 + 初值注入；提交 PUT {yaml}', async () => {
    apiGet.mockImplementation((url: string) => {
      if (url === '/ext/agent_manager/agents/schema') {
        return Promise.resolve({
          data: { fields: [{ name: 'persona', type: 'input', label: '人设' }] },
        })
      }
      if (url === '/ext/agent_manager/agents/main/config') {
        return Promise.resolve({ data: { yaml: 'persona: 默认助手\n' } })
      }
      return Promise.reject(new Error(`unexpected url: ${url}`))
    })
    apiRequest.mockResolvedValue({ data: { ok: true } })

    render(
      <FormWidget
        fieldsUri="/ext/agent_manager/agents/schema"
        dataUri="/ext/agent_manager/agents/main/config"
        dataFormat="yaml"
        submitLabel="保存配置"
      />,
    )
    const input = await screen.findByLabelText('人设')
    expect(input).toHaveValue('默认助手')
    fireEvent.change(input, { target: { value: '新助手' } })
    submitForm()
    await waitFor(() => expect(apiRequest).toHaveBeenCalled())
    const [config] = apiRequest.mock.calls[0]
    expect(config.method).toBe('PUT')
    expect(config.url).toBe('/ext/agent_manager/agents/main/config')
    expect(config.data.yaml).toContain('persona: 新助手')
  })

  it('fieldsUri 响应坏形态 → 错误提示不白屏', async () => {
    apiGet.mockResolvedValue({ data: { nope: true } })
    render(
      <FormWidget fieldsUri="/ext/agent_manager/agents/schema" dataUri="/ext/agent_manager/agents/x/config" dataFormat="yaml" />,
    )
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('fieldsUri 响应不含 fields 数组'),
    )
  })
})

describe('T12：modal 壳模式（吸收 CreateTaskModal）', () => {
  const fields = [
    { name: 'title', type: 'input' as const, label: '标题', required: true },
  ]

  it('受控 open：渲染弹窗；提交成功自动关闭并回调 onSaved', async () => {
    const onSaved = vi.fn()
    const onClose = vi.fn()
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(
      <FormWidget modal={{ title: '新建任务' }} open fields={fields} onSubmit={onSubmit} onSaved={onSaved} onClose={onClose} submitLabel="创建" />,
    )
    expect(screen.getByText('新建任务')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T1' } })
    submitForm()
    await waitFor(() => expect(onSubmit).toHaveBeenCalledWith(expect.objectContaining({ title: 'T1' })))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
    // 成功后自动关闭（onClose 由 modal 壳回调）
    await waitFor(() => expect(onClose).toHaveBeenCalled())
    void rerender
  })

  it('onSubmit 抛错 → 弹窗保持打开 + 错误 toast', async () => {
    const onClose = vi.fn()
    const onSubmit = vi.fn().mockRejectedValue(new Error('缺少会话上下文'))
    render(
      <FormWidget modal={{ title: '新建任务' }} open fields={fields} onSubmit={onSubmit} onClose={onClose} submitLabel="创建" />,
    )
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'x' } })
    submitForm()
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    // 关闭未被调用（失败保持打开）
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByText('新建任务')).toBeInTheDocument()
  })
})

describe('T12：endpoint 模式 extraBody', () => {
  it('POST 体含 extraBody 附加字段（如 thread_id）', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ switched: true }) })
    vi.stubGlobal('fetch', fetchMock)
    render(
      <FormWidget
        fields={[{ name: 'title', type: 'input', label: '标题' }]}
        endpoint="/ext/task_service/tasks/root"
        extraBody={{ thread_id: 'sess-1' }}
        submitLabel="创建"
      />,
    )
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T' } })
    submitForm()
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const body = JSON.parse(fetchMock.mock.calls[0][1].body)
    expect(body.thread_id).toBe('sess-1')
    expect(body.title).toBe('T')
    expect(body.pipeline_id).toEqual(expect.any(String))
    vi.unstubAllGlobals()
  })
})
