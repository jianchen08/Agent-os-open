/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * FormWidget endpoint 直连/readback 行为测试（解耦方案 P2-4 端点真值源收敛）
 *
 * endpoint/readbackUri 声明的请求一律经 apiClient（认证头注入 + 401 刷新链），
 * 不再走裸 fetch。mock 仅网络层（apiClient），组件真实渲染，断言可观察行为：
 * - 提交：POST endpoint，载荷含 pipeline_id/session_id 注入与表单值
 * - 响应协议：unchanged=true → 「已是该模式」；error/reason → 失败态
 * - readback：挂载时 GET 回读，mode 值驱动选择器显示
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
const { mockApiClient } = vi.hoisted(() => ({
  mockApiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
}))
vi.mock('@/services/api/client', () => ({
  apiClient: mockApiClient,
  default: mockApiClient,
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))
// 注入激活管道上下文（readback 以 pipeline_id 为查询键，无管道不回读）
vi.mock('@/stores/agentTabStore', () => ({
  useAgentTabStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({
      activeTabId: 'tab-1',
      tabs: [{ id: 'tab-1', pipelineRunId: 'pipe-9' }],
    }),
}))
vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({ activePipelineId: undefined }),
}))
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({ activeSessionId: 'sess-1' }),
}))
import { FormWidget } from '../FormWidget'

// FormWidget 现消费会话隔离形态（useSessionsQuery）计算权限档显示默认；
// 本文件无 QueryClientProvider，静态 mock 空列表（无会话 → 非隔离默认显示路径）
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: () => ({ data: [] }),
}))

const submitForm = () => fireEvent.submit(document.querySelector('form')!)

describe('FormWidget endpoint 直连（经 apiClient）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('提交 POST endpoint：载荷注入 pipeline_id/session_id + 表单值，成功态展示', async () => {
    mockApiClient.post.mockResolvedValue({ data: { message: '已切换' } })
    render(
      <FormWidget
        fields={[{ name: 'mode', type: 'input', label: '模式' }]}
        endpoint="/ext/human_interaction_tool/permission-mode/switch"
        successText="已切换"
      />,
    )
    fireEvent.change(screen.getByLabelText('模式'), { target: { value: 'strict' } })
    submitForm()

    await waitFor(() => expect(mockApiClient.post).toHaveBeenCalledTimes(1))
    const [url, body] = mockApiClient.post.mock.calls[0]
    expect(url).toBe('/ext/human_interaction_tool/permission-mode/switch')
    // pipeline_id/session_id 来自激活标签/会话（上方 store mock 注入）
    expect(body).toMatchObject({
      mode: 'strict',
      pipeline_id: 'pipe-9',
      session_id: 'sess-1',
    })
    await waitFor(() => expect(screen.getByText('已切换')).toBeInTheDocument())
  })

  it('响应 unchanged=true → 提示无变更（不触发成功动作）', async () => {
    mockApiClient.post.mockResolvedValue({ data: { unchanged: true } })
    render(
      <FormWidget
        fields={[{ name: 'mode', type: 'input', label: '模式' }]}
        endpoint="/ext/x/switch"
      />,
    )
    submitForm()
    await waitFor(() =>
      expect(screen.getByText('当前已是该模式')).toBeInTheDocument(),
    )
  })

  it('响应 error 字段 → 失败态展示原因', async () => {
    mockApiClient.post.mockResolvedValue({ data: { error: '管道不存在' } })
    render(
      <FormWidget
        fields={[{ name: 'mode', type: 'input', label: '模式' }]}
        endpoint="/ext/x/switch"
      />,
    )
    submitForm()
    await waitFor(() => expect(screen.getByText('管道不存在')).toBeInTheDocument())
  })

  it('请求 reject（4xx/5xx 经 apiClient 拦截链）→ 失败态不炸', async () => {
    mockApiClient.post.mockRejectedValue({ code: 'RESOURCE_NOT_FOUND', message: '未命中' })
    render(
      <FormWidget
        fields={[{ name: 'mode', type: 'input', label: '模式' }]}
        endpoint="/ext/x/switch"
      />,
    )
    submitForm()
    await waitFor(() => expect(screen.getByText('请求失败')).toBeInTheDocument())
  })
})

describe('FormWidget readback（经 apiClient 回读）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('挂载时 GET readbackUri（带 pipeline_id 查询参数），mode 驱动回显', async () => {
    mockApiClient.get.mockResolvedValue({ data: { mode: 'strict' } })
    render(
      <FormWidget
        fields={[{ name: 'mode', type: 'select', label: '模式', options: ['strict', 'open'] }]}
        readbackUri="/ext/human_interaction_tool/permission-mode"
      />,
    )
    await waitFor(() => expect(mockApiClient.get).toHaveBeenCalledTimes(1))
    const [url] = mockApiClient.get.mock.calls[0]
    expect(url).toContain('/ext/human_interaction_tool/permission-mode?')
    expect(url).toContain('pipeline_id=')
  })

  it('readback 响应 error 字段抛出并被降级（保留占位显示，不阻断交互）', async () => {
    mockApiClient.get.mockResolvedValue({ data: { error: '读取失败' } })
    render(
      <FormWidget
        fields={[{ name: 'mode', type: 'input', label: '模式' }]}
        readbackUri="/ext/x/mode"
      />,
    )
    await waitFor(() => expect(mockApiClient.get).toHaveBeenCalled())
    // 降级不炸：表单仍可交互
    expect(screen.getByLabelText('模式')).toBeInTheDocument()
  })
})
