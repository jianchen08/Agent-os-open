/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * FormWidget 目标管道契约测试（BUG-22 触发器创建 502 + 静默失败）
 *
 * createSession 表单以管道为作用对象（触发器创建等），字段提示声明
 * 「留空则提交给当前激活管道」：
 * - 无任何管道坐标（注入空 + 表单未选）→ 本地拦截给行动指引，不发必败请求；
 * - 表单 pipeline_id 留空不得覆盖注入的当前激活管道（空值回落）；
 * - endpoint 提交失败必须展示后端真实原因（禁吞成笼统『请求失败』）。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
const { mockApiClient, mockTabState } = vi.hoisted(() => ({
  mockApiClient: { get: vi.fn(), post: vi.fn(), put: vi.fn() },
  mockTabState: { activeTabId: null as string | null, tabs: [] as Array<Record<string, unknown>> },
}))
vi.mock('@/services/api/client', () => ({
  apiClient: mockApiClient,
  default: mockApiClient,
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))
vi.mock('@/stores/agentTabStore', () => ({
  useAgentTabStore: (selector: (s: typeof mockTabState) => unknown) => selector(mockTabState),
}))
vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({ activePipelineId: undefined }),
}))
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: (selector: (s: Record<string, unknown>) => unknown) =>
    selector({ activeSessionId: 'sess-1' }),
}))
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: () => ({ data: [] }),
}))
// createSession 挂载 SessionEditModal（自持 QueryClient 依赖）——本文件只测表单
// 提交契约，弹窗存亡不参与断言，桩掉避免查询Provider环境依赖
vi.mock('@/components/session/SessionEditModal', () => ({
  SessionEditModal: () => null,
}))
import { FormWidget } from '../FormWidget'

const submitForm = () => fireEvent.submit(document.querySelector('form')!)

beforeEach(() => {
  vi.clearAllMocks()
  mockTabState.activeTabId = null
  mockTabState.tabs = []
})

describe('FormWidget createSession 表单的目标管道契约（BUG-22）', () => {
  it('无激活管道且表单未选目标管道 → 本地拦截报错，不发必败请求', async () => {
    render(
      <FormWidget
        fields={[{ name: 'message', type: 'input', label: '触发消息', required: true }]}
        endpoint="/ext/trigger_setup_tool/triggers"
        createSession
      />,
    )
    fireEvent.change(screen.getByLabelText('触发消息'), { target: { value: '到点了' } })
    submitForm()

    await waitFor(() =>
      expect(screen.getByTestId('form-widget-status').textContent).toContain('没有激活管道'),
    )
    expect(mockApiClient.post).not.toHaveBeenCalled()
  })

  it('表单 pipeline_id 留空 → 提交载荷回落注入的当前激活管道（不被空值覆盖）', async () => {
    mockTabState.activeTabId = 'tab-1'
    mockTabState.tabs = [{ id: 'tab-1', pipelineRunId: 'pipe-9' }]
    mockApiClient.post.mockResolvedValue({ data: { message: 'ok' } })
    render(
      <FormWidget
        fields={[
          { name: 'pipeline_id', type: 'select', label: '目标管道', options: [{ label: 'P9', value: 'pipe-9' }] },
          { name: 'message', type: 'input', label: '触发消息' },
        ]}
        endpoint="/ext/trigger_setup_tool/triggers"
        createSession
      />,
    )
    fireEvent.change(screen.getByLabelText('触发消息'), { target: { value: '到点了' } })
    submitForm()

    await waitFor(() => expect(mockApiClient.post).toHaveBeenCalledTimes(1))
    const [, body] = mockApiClient.post.mock.calls[0]
    // 「留空则提交给当前激活管道」：注入值兜底，不被未选择的空表单字段抹掉
    expect(body.pipeline_id).toBe('pipe-9')
  })

  it('选择目标管道后无激活管道 → 按所选管道正常提交（拦截不误伤）', async () => {
    mockApiClient.post.mockResolvedValue({ data: { message: 'ok' } })
    render(
      <FormWidget
        fields={[
          {
            name: 'pipeline_id',
            type: 'select',
            label: '目标管道',
            options: [{ label: 'P7', value: 'pipe-7' }],
          },
          { name: 'message', type: 'input', label: '触发消息' },
        ]}
        endpoint="/ext/trigger_setup_tool/triggers"
        createSession
      />,
    )
    fireEvent.change(screen.getByLabelText('目标管道'), { target: { value: 'pipe-7' } })
    fireEvent.change(screen.getByLabelText('触发消息'), { target: { value: '到点了' } })
    submitForm()

    await waitFor(() => expect(mockApiClient.post).toHaveBeenCalledTimes(1))
    const [, body] = mockApiClient.post.mock.calls[0]
    expect(body.pipeline_id).toBe('pipe-7')
  })
})

describe('FormWidget endpoint 失败反馈必须携带真实原因（BUG-22 静默失败）', () => {
  it('POST reject（Error 实例）→ 状态区展示后端错误文案而非笼统「请求失败」', async () => {
    mockApiClient.post.mockRejectedValue(new Error('缺少注入参数: pipeline_id'))
    render(
      <FormWidget
        fields={[{ name: 'message', type: 'input', label: '触发消息' }]}
        endpoint="/ext/trigger_setup_tool/triggers"
      />,
    )
    submitForm()

    await waitFor(() =>
      expect(screen.getByTestId('form-widget-status').textContent).toBe(
        '缺少注入参数: pipeline_id',
      ),
    )
  })
})
