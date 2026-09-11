/** @feature B9 发送受理协议 | @ci: frontend-test
 * 功能测试：ChatInput 仅在发送回调受理（true/void）时清空输入与草稿
 *
 * 受理协议（SendMessageReceipt）：onSendMessage 返回 false = 未受理
 * （管道未就绪/子标签不支持等），输入与草稿保留供用户重试——修复前回调
 * 返回后无条件清空，pid 解析失败的静默丢弃会连带毁掉用户输入。
 */
import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
const getModelsMock = vi.fn()
vi.mock('@/services/api/config', () => ({
  getModels: (...args: unknown[]) => getModelsMock(...args),
}))
// 只走 staticModel 展示路径：模型注册表保持挂起，避免测试结束后异步
// resolve 触发 act() 告警噪声
getModelsMock.mockImplementation(() => new Promise(() => {}))
const fetchStatesMock = vi.fn()
vi.mock('@/services/api/pipelines', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  fetchPipelineStates: (...args: unknown[]) => fetchStatesMock(...args),
}))
fetchStatesMock.mockImplementation(() => new Promise(() => {}))
import { useChatInputStore } from '@/stores/chatInputStore'
import { renderWithProviders as render } from '@/test/renderWithProviders'
import { ChatInput } from '../ChatInput'

vi.mock('@/hooks/useModelCapabilities', () => ({
  useModelCapabilities: () => ({
    inputCapabilities: {
      canDragDrop: true,
      canPasteImage: true,
      showAttachmentButton: true,
    },
    capabilities: { supportsAudio: false },
  }),
}))

vi.mock('@/hooks/useVoiceInput', () => ({
  useVoiceInput: () => ({
    isSupported: false,
    isRecording: false,
    state: 'idle',
    error: null,
    recordingDuration: 0,
    startRecording: vi.fn(),
    stopRecording: vi.fn(),
  }),
}))

vi.mock('../VoiceInputButton', () => ({
  VoiceInputButton: () => <div data-testid="mock-voice-button" />,
}))

vi.mock('../ChatInputActions', () => ({
  ChatInputActions: () => <div data-testid="mock-chat-input-actions" />,
}))

function renderInput(onSendMessage: (params: unknown) => boolean | void, draftKey?: string) {
  return render(
    <ChatInput
      mode="full"
      // 替身签名收窄为受理协议本身，聚焦发送受理行为
      onSendMessage={onSendMessage as never}
      enableFileUpload
      enableDragDrop
      modelName="deepseek-v3"
      draftKey={draftKey}
    />,
  )
}

const textareaValue = () => (screen.getByTestId('chat-input-textarea') as HTMLTextAreaElement).value

function typeAndSend(text: string) {
  fireEvent.change(screen.getByTestId('chat-input-textarea'), { target: { value: text } })
  fireEvent.click(screen.getByTestId('chat-send-button'))
}

describe('ChatInput 发送受理协议（false 保留输入，true/void 清空）', () => {
  it('回调返回 false（未受理）→ 输入与草稿保留，可直接重试', () => {
    const onSendMessage = vi.fn(() => false)
    renderInput(onSendMessage, 'draft-keep-1')

    typeAndSend('这条消息不能丢')

    expect(onSendMessage).toHaveBeenCalledTimes(1)
    expect(onSendMessage).toHaveBeenCalledWith(expect.objectContaining({ content: '这条消息不能丢' }))
    expect(textareaValue()).toBe('这条消息不能丢')
    expect(useChatInputStore.getState().drafts['draft-keep-1']).toBe('这条消息不能丢')
  })

  it('回调返回 true（受理）→ 清空输入与草稿', () => {
    const onSendMessage = vi.fn(() => true)
    renderInput(onSendMessage, 'draft-accept-1')

    typeAndSend('正常发送')

    expect(onSendMessage).toHaveBeenCalledTimes(1)
    expect(textareaValue()).toBe('')
    expect(useChatInputStore.getState().drafts['draft-accept-1']).toBeUndefined()
  })

  it('回调无返回值（void，兼容既有只发不回的回调）→ 照常清空（回归）', () => {
    const onSendMessage = vi.fn()
    renderInput(onSendMessage, 'draft-void-1')

    typeAndSend('void 兼容发送')

    expect(onSendMessage).toHaveBeenCalledTimes(1)
    expect(textareaValue()).toBe('')
    expect(useChatInputStore.getState().drafts['draft-void-1']).toBeUndefined()
  })
})
