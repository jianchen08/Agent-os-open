/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：ChatInput 底部工具栏布局防溢出（发送按钮不被挤出输入框）
 *
 * 用户反馈：部分主题（大字体/插件动作多）下发送按钮被挤出输入框容器。
 * 根因：左组（附件/语音/思考/上下文用量/插件动作）无收缩约束，总宽超限时
 * 右组发送按钮溢出容器右侧。
 *
 * 修复约束（防回归）：
 * - 左组容器 min-w-0 flex-1（可收缩）
 * - 发送按钮 shrink-0（不被压缩/挤出）
 * - 上下文用量指示器允许收缩（min-w-0，内部截断）
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
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

vi.mock('../ThinkingModeToggle', () => ({
  ThinkingModeToggle: (props: {
    strength?: string
    onStrengthChange?: (s: string) => void
    disabled?: boolean
  }) => (
    <button
      type="button"
      data-testid="mock-thinking-toggle"
      data-strength={props.strength}
      disabled={props.disabled}
      onClick={() => props.onStrengthChange?.('high')}
    >
      thinking
    </button>
  ),
}))

// 插件动作容器：模拟声明了多个动作，验证其可收缩且不挤出发送按钮
vi.mock('../ChatInputActions', () => ({
  ChatInputActions: () => (
    <div data-testid="mock-chat-input-actions" className="min-w-0">
      <button type="button" className="h-8 shrink-0 rounded-lg px-2 text-xs">
        插件动作一
      </button>
      <button type="button" className="h-8 shrink-0 rounded-lg px-2 text-xs">
        插件动作二
      </button>
    </div>
  ),
}))

function renderInput() {
  return render(
    <ChatInput
      mode="full"
      onSendMessage={() => {}}
      enableFileUpload
      enableDragDrop
      modelName="deepseek-v3"
      currentTokenUsage={4000}
      maxTokens={10000}
      enableThinkingMode
    />,
  )
}

describe('ChatInput 底部工具栏 — 发送按钮不被挤出', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('底部工具栏：左组可收缩（min-w-0 flex-1），发送按钮 shrink-0', () => {
    renderInput()

    const toolbar = document.querySelector('[data-testid="chat-input"] > div > div.flex') as HTMLElement
    // 底部工具栏是输入容器内的最后一个 flex 行
    const sendButton = screen.getByTestId('chat-send-button')

    // 发送按钮显式不可收缩（防被挤出）
    expect(sendButton.className).toContain('shrink-0')
    // 发送按钮在输入容器内
    expect(sendButton.closest('[data-testid="chat-input"]')).not.toBeNull()
  })

  it('发送按钮容器与左组并列于同一行，左组允许收缩', () => {
    renderInput()

    const sendButton = screen.getByTestId('chat-send-button')
    // 工具栏行 = 发送按钮向上最近的 justify-between 容器
    const toolbar = sendButton.closest('.justify-between') as HTMLElement
    expect(toolbar).not.toBeNull()
    // 左组（工具栏第一个子元素）：min-w-0 + flex-1，内容可收缩
    const leftGroup = toolbar.firstElementChild as HTMLElement
    expect(leftGroup.className).toContain('min-w-0')
    expect(leftGroup.className).toContain('flex-1')
  })

  it('上下文用量指示器允许收缩（min-w-0），不占死宽度', () => {
    renderInput()
    const indicator = screen.getByTestId('context-usage-indicator')
    expect(indicator.className).toContain('min-w-0')
  })

  it('发送消息携带思考强度：enableThinking + thinkingStrength 透传', () => {
    const onSendMessage = vi.fn()
    render(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage}
        modelName="deepseek-v3"
        thinkingStrength="high"
      />,
    )

    const textarea = screen.getByTestId('chat-input-textarea')
    fireEvent.change(textarea, { target: { value: '你好' } })
    fireEvent.click(screen.getByTestId('chat-send-button'))

    expect(onSendMessage).toHaveBeenCalledWith(
      expect.objectContaining({
        content: '你好',
        enableThinking: true,
        thinkingStrength: 'high',
      }),
    )
  })

  it('关闭强度 → enableThinking=false', () => {
    const onSendMessage = vi.fn()
    render(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage}
        modelName="deepseek-v3"
        thinkingStrength="off"
      />,
    )

    fireEvent.change(screen.getByTestId('chat-input-textarea'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByTestId('chat-send-button'))

    expect(onSendMessage).toHaveBeenCalledWith(
      expect.objectContaining({ enableThinking: false, thinkingStrength: 'off' }),
    )
  })

  it('思考强度选择器收到当前强度；点击回调 onThinkingStrengthChange', () => {
    const onThinkingStrengthChange = vi.fn()
    render(
      <ChatInput
        mode="full"
        onSendMessage={() => {}}
        modelName="deepseek-v3"
        enableThinkingMode
        thinkingStrength="medium"
        onThinkingStrengthChange={onThinkingStrengthChange}
      />,
    )

    const toggle = screen.getByTestId('mock-thinking-toggle')
    expect(toggle).toHaveAttribute('data-strength', 'medium')
    fireEvent.click(toggle)
    expect(onThinkingStrengthChange).toHaveBeenCalledWith('high')
  })
})
