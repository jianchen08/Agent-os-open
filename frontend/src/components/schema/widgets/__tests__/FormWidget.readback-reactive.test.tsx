/**
 * readback 回读随执行上下文（pipelineId/sessionId）变化重发的行为测试
 *
 * readbackUri 的 GET query 拼接当前 pipeline_id 与 session_id——执行上下文
 * 变化后必须以新值重发回读，否则选择器显示的是旧管道/旧会话的当前值。
 * mock 仅网络层（apiClient），store 用真实 zustand 实例驱动状态变化。
 */
import { act, render, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { FormWidget } from '../FormWidget'

const apiGet = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(() => undefined, {
    get: (...args: unknown[]) => apiGet(...args),
    post: vi.fn(),
  }),
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

const selectFields = [
  {
    name: 'mode',
    type: 'select' as const,
    label: '权限模式',
    options: [
      { label: '默认档', value: 'default' },
      { label: '旁路档', value: 'bypass' },
    ],
  },
]

beforeEach(() => {
  apiGet.mockReset()
  apiGet.mockResolvedValue({ data: { mode: 'bypass' } })
  useAgentTabStore.setState({ activeTabId: null, tabs: [] })
  usePipelineMessageStore.setState({ activePipelineId: null })
  useSessionStore.setState({ activeSessionId: null })
})

describe('readback：pipeline_id/session_id 参与 query，变化即重发', () => {
  it('无激活管道不回读；管道激活后按新 pipeline_id 回读；会话切换补 session_id', async () => {
    render(<FormWidget fields={selectFields} onChange={vi.fn()} readbackUri="/ext/perm/mode" />)

    // 无激活管道：回读 guard 不发请求
    expect(apiGet).not.toHaveBeenCalled()

    // 激活管道 → 以新 pipeline_id 重发回读
    act(() => {
      usePipelineMessageStore.setState({ activePipelineId: 'pipe-A' })
    })
    await waitFor(() => expect(apiGet).toHaveBeenCalledWith('/ext/perm/mode?pipeline_id=pipe-A'))

    // 会话切换 → 回读带 session_id 重新拼接 query
    act(() => {
      useSessionStore.setState({ activeSessionId: 'sess-1' })
    })
    await waitFor(() =>
      expect(apiGet).toHaveBeenCalledWith('/ext/perm/mode?pipeline_id=pipe-A&session_id=sess-1'),
    )
  })
})
