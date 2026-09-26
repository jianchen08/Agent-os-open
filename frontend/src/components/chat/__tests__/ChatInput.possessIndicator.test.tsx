/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * ChatInput — 附身指示条（roleplay.possess 桥宿主侧可视化）
 *
 * 核验：未附身零渲染；附身期间 chip 常驻展示卡名；点「解除」→ store 清档 +
 * chip 消失（发送链随之不带附身键——roleplay_persona 注入逻辑在
 * modeOptions.withRoleplayPersona 车道覆盖）。
 *
 * mock 仅外部依赖：管道 state 查询（网络）、会话查询（网络）、语音输入 hook
 * （浏览器媒体 API）。
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useRoleplayPossessStore } from '@/stores/roleplayPossessStore'
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
})

describe('ChatInput — 附身指示条', () => {
  it('未附身 → 零渲染', () => {
    renderWithProviders(<ChatInput onSendMessage={() => true} modelName={undefined} />)
    expect(screen.queryByTestId('possess-indicator')).not.toBeInTheDocument()
  })

  it('已附身 → chip 常驻展示「已附身：<卡名>」', () => {
    useRoleplayPossessStore
      .getState()
      .setPossessed({ card_id: 'card_luna', name: '月见', avatar: '🌙' })
    renderWithProviders(<ChatInput onSendMessage={() => true} modelName={undefined} />)

    const chip = screen.getByTestId('possess-indicator')
    expect(chip).toHaveTextContent('已附身：月见')
    expect(screen.getByRole('button', { name: '解除附身' })).toBeInTheDocument()
  })

  it('点「解除」→ store 清档 + chip 消失', async () => {
    useRoleplayPossessStore
      .getState()
      .setPossessed({ card_id: 'card_rin', name: '凛', avatar: '🎭' })
    renderWithProviders(<ChatInput onSendMessage={() => true} modelName={undefined} />)

    fireEvent.click(screen.getByRole('button', { name: '解除附身' }))

    await waitFor(() => {
      expect(useRoleplayPossessStore.getState().possessed).toBeNull()
    })
    expect(screen.queryByTestId('possess-indicator')).not.toBeInTheDocument()
    // 解除即清持久化档（重启后不复活）
    expect(localStorage.getItem('roleplay.possessed')).toContain('null')
  })
})
