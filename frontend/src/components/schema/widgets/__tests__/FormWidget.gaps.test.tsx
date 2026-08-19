/**
 * 交互缺口补齐测试（G1/G2）
 *
 * G1（反馈文案/成功动作）：FormWidget successText/failureText/successAction
 * 声明化 + endpoint 提交协议（无 error/reason 即成功，通用表单端点不再误判失败）
 * G2（级联选择）：datasourceUri {{字段}} 模板随表单值渲染 + dependsOn 依赖变更
 * 自动重拉（fetchDatasourceOptions 对绝对 URI 走 apiClient.get——以 apiGet 断言）
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

import { FormWidget } from '../FormWidget'
import { RjsfForm } from '@/services/schema/RjsfForm'

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

const textField = (name: string, label: string) =>
  ({ name, type: 'input' as const, label })

beforeEach(() => {
  apiGet.mockReset()
  apiRequest.mockReset()
})

describe('G1：反馈文案/成功动作声明化', () => {
  it('successText 覆盖提交成功文案', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(
      <FormWidget fields={[textField('title', '标题')]} onSubmit={onSubmit} successText="任务下发成功" />,
    )
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T' } })
    submitForm()
    await waitFor(() => expect(screen.getByText('任务下发成功')).toBeInTheDocument())
  })

  it('failureText 覆盖失败文案（onSubmit 抛错）', async () => {
    render(
      <FormWidget
        fields={[textField('x', 'X')]}
        onSubmit={vi.fn().mockRejectedValue(new Error('boom'))}
        failureText="自定义失败文案"
      />,
    )
    fireEvent.change(screen.getByLabelText('X'), { target: { value: 'v' } })
    submitForm()
    await waitFor(() => expect(screen.getByText('自定义失败文案')).toBeInTheDocument())
  })

  it('endpoint 响应无 error/reason → 成功（通用表单端点不再误判失败）', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ task_id: 't-1' }) })
    vi.stubGlobal('fetch', fetchMock)
    render(
      <FormWidget
        fields={[textField('title', '标题')]}
        endpoint="/ext/channel_api/tasks/root"
        submitLabel="创建"
        successText="已创建任务"
      />,
    )
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T' } })
    submitForm()
    await waitFor(() =>
      expect(screen.getByTestId('form-widget-status').textContent).toBe('已创建任务'),
    )
    vi.unstubAllGlobals()
  })

  it('successAction.open_panel：成功后按声明路径打开面板（不抛错）', async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(
      <FormWidget
        fields={[textField('title', '标题')]}
        onSubmit={onSubmit}
        successAction={{ type: 'open_panel', path: '/cost' }}
      />,
    )
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T' } })
    submitForm()
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
  })

  it('successAction.reload：成功后 datasource 重拉（再次 GET）', async () => {
    apiGet.mockImplementation((url: string) =>
      Promise.resolve({ data: { fields: [textField('v', 'V')] } }),
    )
    apiRequest.mockResolvedValue({ data: {} })
    render(
      <FormWidget
        fieldsUri="/ext/agent_manager/agents/schema"
        dataUri="/ext/agent_manager/agents/x/config"
        dataFormat="json"
        successAction={{ type: 'reload' }}
      />,
    )
    await screen.findByLabelText('V')
    const getsBefore = apiGet.mock.calls.length
    submitForm()
    await waitFor(() => expect(apiRequest).toHaveBeenCalled())
    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThan(getsBefore))
  })
})

describe('G2：级联选择（datasourceUri 模板 + dependsOn 重拉）', () => {
  it('{{field}} 模板随表单值渲染并重拉（文本字段驱动，jsdom 可靠）', async () => {
    apiGet.mockResolvedValue({ data: [] })
    render(
      <RjsfForm
        fields={[
          { name: 'provider', type: 'input' as const, label: '提供商' },
          {
            name: 'model',
            type: 'select' as const,
            label: '模型',
            datasourceUri: '/ext/opts?provider={{provider}}',
          },
        ]}
      />,
    )
    // 初始空值 → 模板空段
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/ext/opts?provider='))
    // 输入 provider → onChange → formData 更新 → 模板实值重拉
    fireEvent.change(screen.getByLabelText('提供商'), { target: { value: 'zhipu' } })
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/ext/opts?provider=zhipu'))
    // 再改一次 → 依赖变化再次重拉
    fireEvent.change(screen.getByLabelText('提供商'), { target: { value: 'openai' } })
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/ext/opts?provider=openai'))
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/ext/opts?provider=zhipu'))
  })

  it('显式 dependsOn 字段变化触发重拉（datasourceUri 无模板时也生效）', async () => {
    apiGet.mockResolvedValue({ data: [] })
    render(
      <RjsfForm
        fields={[
          { name: 'scope', type: 'input' as const, label: '范围' },
          { name: 'item', type: 'select' as const, label: '条目', datasourceUri: '/ext/items', dependsOn: ['scope'] },
        ]}
      />,
    )
    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThanOrEqual(1))
    // 依赖字段变化 → 同一 URI 重拉（无模板，dependsOn 驱动）
    fireEvent.change(screen.getByLabelText('范围'), { target: { value: 'task' } })
    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThanOrEqual(2))
    fireEvent.change(screen.getByLabelText('范围'), { target: { value: 'session' } })
    await waitFor(() => expect(apiGet.mock.calls.length).toBeGreaterThanOrEqual(3))
    // 依赖值未变（重复输入同值）→ 不重拉
    const stable = apiGet.mock.calls.length
    fireEvent.change(screen.getByLabelText('范围'), { target: { value: 'session' } })
    await new Promise((r) => setTimeout(r, 200))
    expect(apiGet.mock.calls.length).toBe(stable)
  })
})
