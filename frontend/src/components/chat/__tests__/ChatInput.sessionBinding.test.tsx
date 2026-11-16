/** @feature FP-0.2.四 会话身份绑定指示条退出链（存储异常显式报错） | @ci: frontend-test */
/**
 * ChatInput 会话身份绑定退出链路：绑定在场展示指示条；退出成功清空；
 * 本地存储不可用（clearSessionAgentBinding 抛错）→ 操作点显式报错不静默
 * 假退出（2026-10-19 环境故障呈现契约）。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const binder = vi.hoisted(() => ({
  readSessionAgentBinding: vi.fn(),
  clearSessionAgentBinding: vi.fn(),
}))

vi.mock('@/services/modeSessionBinder', () => ({
  readSessionAgentBinding: (...args: unknown[]) => binder.readSessionAgentBinding(...args),
  clearSessionAgentBinding: (...args: unknown[]) => binder.clearSessionAgentBinding(...args),
}))

const sessionState = vi.hoisted(() => ({ activeSessionId: 's-1' as string | null }))
vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: (sel: (s: { activeSessionId: string | null }) => unknown) =>
    sel({ activeSessionId: sessionState.activeSessionId }),
}))

vi.mock('@/services/api/config', () => ({
  getModels: vi.fn(() => new Promise(() => {})),
  getLLMPresets: vi.fn(() => new Promise(() => {})),
}))
vi.mock('@/services/api/pipelines', () => ({
  fetchPipelineStates: vi.fn(() => new Promise(() => {})),
}))
vi.mock('@/hooks/useModelCapabilities', () => ({
  useModelCapabilities: () => ({
    inputCapabilities: { canDragDrop: false, canPasteImage: false, showAttachmentButton: false },
    capabilities: { supportsAudio: false },
  }),
}))
vi.mock('@/hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({
    isSupported: false, isRecording: false, state: 'idle', mode: 'browser',
    recordingDuration: 0, error: null, startRecording: vi.fn(), stopRecording: vi.fn(),
  }),
}))
vi.mock('../VoiceInputButton', () => ({ VoiceInputButton: () => null }))
vi.mock('../ChatInputActions', () => ({ ChatInputActions: () => null }))
vi.mock('../ChatInputPossessChip', () => ({ ChatInputPossessChip: () => null }))

import { renderWithProviders } from '@/test/renderWithProviders'
import { ChatInput } from '../ChatInput'
import type { ChatInputProps } from '../ChatInput'

function renderInput() {
  const props: ChatInputProps = {
    mode: 'full',
    onSendMessage: () => {},
    enableFileUpload: false,
    enableDragDrop: false,
    modelName: 'm',
  }
  return renderWithProviders(<ChatInput {...props} />)
}

beforeEach(() => {
  sessionState.activeSessionId = 's-1'
  binder.readSessionAgentBinding.mockReset()
  binder.clearSessionAgentBinding.mockReset()
})

describe('会话身份绑定指示条', () => {
  it('绑定在场：展示指示条；退出成功 → 指示条消失', async () => {
    binder.readSessionAgentBinding.mockReturnValue({ name: '研究员' })
    binder.clearSessionAgentBinding.mockReturnValue(undefined)
    renderInput()
    const indicator = await screen.findByTestId('session-agent-indicator')
    expect(indicator).toHaveTextContent('会话身份：研究员')
    fireEvent.click(screen.getByRole('button', { name: '退出会话身份绑定' }))
    await waitFor(() =>
      expect(screen.queryByTestId('session-agent-indicator')).toBeNull(),
    )
  })

  it('本地存储不可用：退出报显式错误，不静默假退出', async () => {
    binder.readSessionAgentBinding.mockReturnValue({ name: '研究员' })
    binder.clearSessionAgentBinding.mockImplementation(() => {
      throw new Error('quota exceeded')
    })
    renderInput()
    await screen.findByTestId('session-agent-indicator')
    fireEvent.click(screen.getByRole('button', { name: '退出会话身份绑定' }))
    await waitFor(() =>
      expect(screen.getByText(/退出会话身份绑定失败：quota exceeded/)).toBeInTheDocument(),
    )
  })
})
