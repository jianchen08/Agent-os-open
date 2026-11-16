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

describe('ChatInput 发送受理协议（false 保留输入；true/void 受理进入发送态，清空只在落定）', () => {
  it('回调返回 false（未受理）→ 输入与草稿保留，可直接重试', () => {
    const onSendMessage = vi.fn(() => false)
    renderInput(onSendMessage, 'draft-keep-1')

    typeAndSend('这条消息不能丢')

    expect(onSendMessage).toHaveBeenCalledTimes(1)
    expect(onSendMessage).toHaveBeenCalledWith(expect.objectContaining({ content: '这条消息不能丢' }))
    expect(textareaValue()).toBe('这条消息不能丢')
    expect(useChatInputStore.getState().drafts['draft-keep-1']).toBe('这条消息不能丢')
  })

  // 2026-11-04 落定语义重定义：受理 ≠ 清空——发送态终点 = 消息落进消息流
  //（气泡认领/队列条出现），清空由落定订阅回调按快照比对执行
  //（见 ChatInput.sendLifecycle.test.tsx）。
  it('回调返回 true（受理）→ 输入与草稿保留（清空只在落定）', () => {
    const onSendMessage = vi.fn(() => true)
    renderInput(onSendMessage, 'draft-accept-1')

    typeAndSend('正常发送')

    expect(onSendMessage).toHaveBeenCalledTimes(1)
    expect(textareaValue()).toBe('正常发送')
    expect(useChatInputStore.getState().drafts['draft-accept-1']).toBe('正常发送')
  })

  it('回调无返回值（void，兼容既有只发不回的回调）→ 照常受理进入发送态，输入保留（回归）', () => {
    const onSendMessage = vi.fn()
    renderInput(onSendMessage, 'draft-void-1')

    typeAndSend('void 兼容发送')

    expect(onSendMessage).toHaveBeenCalledTimes(1)
    expect(textareaValue()).toBe('void 兼容发送')
    expect(useChatInputStore.getState().drafts['draft-void-1']).toBe('void 兼容发送')
  })
})

describe('ChatInput 未受理显式反馈（OBS-25：拒绝必须落在操作点）', () => {
  it('回调返回 false → 输入区出现「消息未发送」显式提示，输入与草稿保留', () => {
    const onSendMessage = vi.fn(() => false)
    renderInput(onSendMessage, 'draft-reject-feedback-1')

    typeAndSend('无活跃会话时发送')

    // 反馈在输入区可见（role=alert），指明未发送而非静默保留
    expect(screen.getByRole('alert')).toHaveTextContent('消息未发送，请检查会话状态')
    expect(textareaValue()).toBe('无活跃会话时发送')
    expect(useChatInputStore.getState().drafts['draft-reject-feedback-1']).toBe('无活跃会话时发送')
  })

  it('false 后重试受理 → 未受理提示清除（不残留旧错）；提示亦可手动关闭', () => {
    const onSendMessage = vi.fn(() => false)
    renderInput(onSendMessage, 'draft-reject-then-accept')

    typeAndSend('第一次被拒')
    expect(screen.getByRole('alert')).toBeInTheDocument()

    onSendMessage.mockReturnValue(true)
    typeAndSend('第二次重试')
    expect(screen.queryByRole('alert')).toBeNull()

    onSendMessage.mockReturnValue(false)
    typeAndSend('再次被拒')
    expect(screen.getByRole('alert')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '关闭错误提示' }))
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('未受理不制造忙碌假象：不出现「停止生成」按钮，发送按钮保持可见', () => {
    const onSendMessage = vi.fn(() => false)
    renderInput(onSendMessage, 'draft-reject-busy-1')

    typeAndSend('被拒发送')

    expect(screen.queryByTestId('chat-stop-button')).toBeNull()
    expect(screen.getByTestId('chat-send-button')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('消息未发送')
  })
})

describe('ChatInput 发送在途（落定语义：转圈+输入锁定，落定前不回到可发送态）', () => {
  function renderInFlight(sendInFlight: boolean) {
    return render(
      <ChatInput
        mode="full"
        onSendMessage={vi.fn() as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        sendInFlight={sendInFlight}
      />,
    )
  }

  it('sendInFlight → 按钮变转圈等待态（禁用，不可再点发送）', () => {
    renderInFlight(true)
    const inflight = screen.getByTestId('chat-send-inflight')
    expect(inflight).toBeDisabled()
    expect(screen.queryByTestId('chat-send-button')).toBeNull()
  })

  // 2026-11-04 落定语义重定义：发送态期间输入锁定不可编辑（textarea disabled）
  // ——连发节奏受落定耗时限制属可接受代价（用户裁定），不再「键入回落发送态」。
  it('sendInFlight 期间 textarea 锁定不可编辑（锁定=连发节奏受落定耗时限制）', () => {
    renderInFlight(true)
    expect(screen.getByTestId('chat-input-textarea')).toBeDisabled()
    expect(screen.getByTestId('chat-input-textarea').placeholder).toContain('发送中')
  })

  it('无在途 → 正常发送按钮（回归）', () => {
    renderInFlight(false)
    expect(screen.getByTestId('chat-send-button')).toBeInTheDocument()
    expect(screen.queryByTestId('chat-send-inflight')).toBeNull()
  })
})
