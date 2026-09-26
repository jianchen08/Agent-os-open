/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * ChatInput — 扮演会话指示条（roleplay.continue 桥落地的会话执行选项绑定）
 *
 * 核验：无绑定零渲染；绑定存在 chip 常驻展示卡名（agentName 缺席回退 agentId
 * 尾段）；点「退出」→ 快照绑定清空 + chip 消失（发送链随之不带 WS agent_id）；
 * 附身与扮演绑定并存 → 附身 chip 独占（扮演 chip 不渲染，与发送链附身独占同序）。
 *
 * mock 仅外部依赖：管道 state 查询（网络）、会话查询（网络）、语音输入 hook
 * （浏览器媒体 API）。快照存取走真实 localStorage。
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useRoleplayPossessStore } from '@/stores/roleplayPossessStore'
import { useSessionStore } from '@/stores/sessionStore'
import { renderWithProviders } from '@/test/renderWithProviders'
import { ChatInput } from '../ChatInput'

const mocks = vi.hoisted(() => ({
  fetchPipelineStates: vi.fn(),
}))

vi.mock('@/services/api/pipelines', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  fetchPipelineStates: mocks.fetchPipelineStates,
}))
vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: () => ({ data: [] }),
}))
vi.mock('@/hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({
    isSupported: false,
    isRecording: false,
    isTranscribing: false,
    transcript: '',
    recordingDuration: 0,
    state: 'idle',
    mode: 'browser',
    error: null,
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
  }),
}))

beforeEach(() => {
  mocks.fetchPipelineStates.mockResolvedValue({})
  localStorage.clear()
  useRoleplayPossessStore.setState({ possessed: null })
  useSessionStore.setState({ activeSessionId: 'th-rp' })
})

function seedBinding(threadId: string, agentId: string, agentName?: string): void {
  localStorage.setItem(
    `session-exec-options:${threadId}`,
    JSON.stringify(agentName ? { values: {}, agentId, agentName } : { values: {}, agentId }),
  )
}

describe('ChatInput — 扮演会话指示条', () => {
  it('无绑定 → 零渲染', () => {
    renderWithProviders(<ChatInput onSendMessage={() => true} modelName={undefined} />)
    expect(screen.queryByTestId('roleplay-session-indicator')).not.toBeInTheDocument()
  })

  it('绑定存在 → chip 常驻展示「扮演会话：<卡名>」', () => {
    seedBinding('th-rp', 'mode_roleplay/card_luna', '月见')
    renderWithProviders(<ChatInput onSendMessage={() => true} modelName={undefined} />)
    expect(screen.getByTestId('roleplay-session-indicator')).toHaveTextContent('扮演会话：月见')
    expect(screen.getByRole('button', { name: '退出扮演会话' })).toBeInTheDocument()
  })

  it('agentName 缺席 → chip 回退 agentId 尾段', () => {
    seedBinding('th-rp', 'mode_roleplay/card_rin')
    renderWithProviders(<ChatInput onSendMessage={() => true} modelName={undefined} />)
    expect(screen.getByTestId('roleplay-session-indicator')).toHaveTextContent('扮演会话：card_rin')
  })

  it('点「退出」→ 快照绑定清空 + chip 消失（其余快照键保留）', async () => {
    localStorage.setItem(
      'session-exec-options:th-rp',
      JSON.stringify({
        values: { conversation_mode: 'plan' },
        agentId: 'mode_roleplay/card_luna',
        agentName: '月见',
      }),
    )
    renderWithProviders(<ChatInput onSendMessage={() => true} modelName={undefined} />)

    fireEvent.click(screen.getByRole('button', { name: '退出扮演会话' }))

    await waitFor(() => {
      expect(screen.queryByTestId('roleplay-session-indicator')).not.toBeInTheDocument()
    })
    const snapshot = JSON.parse(localStorage.getItem('session-exec-options:th-rp') ?? '{}')
    expect(snapshot).toEqual({ values: { conversation_mode: 'plan' } })
  })

  it('附身与扮演绑定并存 → 附身 chip 独占（扮演 chip 不渲染）', () => {
    seedBinding('th-rp', 'mode_roleplay/card_luna', '月见')
    useRoleplayPossessStore
      .getState()
      .setPossessed({ card_id: 'card_rin', name: '凛', avatar: '🎭' })
    renderWithProviders(<ChatInput onSendMessage={() => true} modelName={undefined} />)

    expect(screen.getByTestId('possess-indicator')).toHaveTextContent('已附身：凛')
    expect(screen.queryByTestId('roleplay-session-indicator')).not.toBeInTheDocument()
  })
})
