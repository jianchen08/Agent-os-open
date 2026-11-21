/** @feature FP-T12 前端适配 @ci: frontend-test
 * ChatInput 录音/识别态与展开编辑器浮钮互斥（移动端视觉 P0-3，MuMu 实测
 * shots/06）：录音横幅显示期间右上角展开/收起浮钮必须退场——横幅右端「取消」
 * 按钮与展开图标同占输入容器右上角，共存即重叠压字。空闲态浮钮恢复。
 */
import { screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { useChatInputStore } from '@/stores/chatInputStore'
import { renderWithProviders as render } from '@/test/renderWithProviders'
import { ChatInput } from '../ChatInput'
import type { ChatInputProps } from '../types'

/** 外部服务替身（vi.hoisted：vi.mock 工厂与用例体共享同一实例） */
const mocks = vi.hoisted(() => ({
  getModels: vi.fn(),
  uploadFile: vi.fn(),
  validateFile: vi.fn(),
  reportError: vi.fn(),
  caps: {
    canDragDrop: true,
    canPasteImage: true,
    showAttachmentButton: true,
    supportsAudio: false,
  },
  voice: {
    isSupported: true,
    isRecording: false,
    state: 'idle',
    mode: 'server-asr',
    recordingDuration: 3,
    error: null as { type: string; message: string } | null,
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
    startServerASR: vi.fn(),
    cancelRecording: vi.fn(),
  },
}))

vi.mock('@/services/api/config', () => ({
  getModels: (...args: unknown[]) => mocks.getModels(...args),
}))
vi.mock('@/services/api/files', () => ({
  uploadFile: (...args: unknown[]) => mocks.uploadFile(...args),
  validateFile: (...args: unknown[]) => mocks.validateFile(...args),
}))
vi.mock('@/services/errorReporting', () => ({
  reportError: (...args: unknown[]) => mocks.reportError(...args),
  ErrorSeverity: { ERROR: 'error', WARNING: 'warning', INFO: 'info' },
  ErrorType: { NETWORK: 'network', VALIDATION: 'validation' },
}))
vi.mock('@/hooks/useModelCapabilities', () => ({
  useModelCapabilities: () => ({
    inputCapabilities: {
      canDragDrop: mocks.caps.canDragDrop,
      canPasteImage: mocks.caps.canPasteImage,
      showAttachmentButton: mocks.caps.showAttachmentButton,
    },
    capabilities: { supportsAudio: mocks.caps.supportsAudio },
  }),
}))
vi.mock('@/hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({
    state: mocks.voice.state,
    isRecording: mocks.voice.isRecording,
    isTranscribing: mocks.voice.state === 'transcribing',
    transcript: '',
    recordingDuration: mocks.voice.recordingDuration,
    mode: mocks.voice.mode,
    error: mocks.voice.error,
    startRecording: mocks.voice.startRecording,
    stopRecording: mocks.voice.stopRecording,
    startServerASR: mocks.voice.startServerASR,
    cancelRecording: mocks.voice.cancelRecording,
    isSupported: mocks.voice.isSupported,
  }),
}))
vi.mock('../VoiceInputButton', () => ({
  VoiceInputButton: () => <button type="button">语音输入替身</button>,
}))

function renderInput(props: Partial<ChatInputProps> = {}) {
  const resolved: ChatInputProps = {
    mode: 'full',
    onSendMessage: () => true,
    enableFileUpload: true,
    enableDragDrop: true,
    modelName: 'deepseek-v3',
    ...props,
  }
  return render(<ChatInput {...resolved} />)
}

beforeEach(() => {
  localStorage.clear()
  useChatInputStore.setState({ pendingInsert: null, drafts: {} })
  Object.assign(mocks.caps, {
    canDragDrop: true,
    canPasteImage: true,
    showAttachmentButton: true,
    supportsAudio: false,
  })
  Object.assign(mocks.voice, {
    isSupported: true,
    isRecording: false,
    state: 'idle',
    mode: 'server-asr',
    recordingDuration: 3,
    error: null,
  })
})

describe('ChatInput 展开浮钮与录音横幅互斥（P0-3）', () => {
  it('录音中：横幅在场，展开浮钮退场（互斥）', () => {
    mocks.voice.isRecording = true
    renderInput()
    expect(screen.getByTestId('composer-recording-banner')).toBeInTheDocument()
    expect(screen.queryByTestId('chat-input-expand-toggle')).not.toBeInTheDocument()
  })

  it('识别中（录音已停止等结果）：横幅在场，展开浮钮退场（互斥）', () => {
    mocks.voice.state = 'transcribing'
    renderInput()
    expect(screen.getByTestId('composer-recording-banner')).toBeInTheDocument()
    expect(screen.queryByTestId('chat-input-expand-toggle')).not.toBeInTheDocument()
  })

  it('空闲：无横幅，展开浮钮恢复在场（区分组）', () => {
    renderInput()
    expect(screen.queryByTestId('composer-recording-banner')).not.toBeInTheDocument()
    expect(screen.getByTestId('chat-input-expand-toggle')).toBeInTheDocument()
  })
})
