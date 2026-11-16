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

import { fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
const getModelsMock = vi.fn()
vi.mock('@/services/api/config', () => ({
  getModels: (...args: unknown[]) => getModelsMock(...args),
}))
// 本文件用例只走 staticModel 展示路径：模型注册表/states 两个数据源保持挂起，
// 避免测试结束后异步 resolve 触发 act() 告警噪声
getModelsMock.mockImplementation(() => new Promise(() => {}))
const fetchStatesMock = vi.fn()
vi.mock('@/services/api/pipelines', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/services/api/pipelines')>()),
  fetchPipelineStates: (...args: unknown[]) => fetchStatesMock(...args),
}))
// 本文件用例只走 staticModel 展示路径：states 查询保持挂起，避免测试结束后
// 异步 resolve 触发 act() 告警噪声
fetchStatesMock.mockImplementation(() => new Promise(() => {}))
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
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

// 思考强度槽位 stub：等价 llm_core 的 ui_schema 声明（chat-input 空间
// thinking_strength 槽位），stub 组件接收宿主 overrideProps 注入的受控值/回调
const ThinkingStrengthStub = (props: Record<string, unknown>) => {
  const value = props.value as { strength?: string } | undefined
  const onChange = props.onChange as ((v: Record<string, unknown>) => void) | undefined
  return (
    <button
      type="button"
      data-testid="mock-thinking-toggle"
      data-strength={value?.strength}
      disabled={Boolean(props.disabled)}
      onClick={() => onChange?.({ strength: 'high' })}
    >
      thinking
    </button>
  )
}

function registerThinkingSlot() {
  widgetRegistry.register('test_thinking_stub', ThinkingStrengthStub, {
    name: 'test_thinking_stub',
    supportedSpaces: ['chat', 'workspace'],
  })
  contributionRegistry.loadFromSchema({
    pipelines: [
      {
        id: 'pipeline_llm_core',
        ui_schema: {
          widgets: [
            { id: 'thinking_strength', type: 'test_thinking_stub', space: 'chat-input' },
          ],
        },
      },
    ],
  } as never)
}

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
      enableThinkingMode
    />,
  )
}

describe('ChatInput 底部工具栏 — 发送按钮不被挤出', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    widgetRegistry.clear()
    contributionRegistry.clear()
    registerThinkingSlot()
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

  it('窄栏防叠压：左组带 flex-wrap，放不下的按钮换行排布而非叠压/溢出', () => {
    renderInput()

    const sendButton = screen.getByTestId('chat-send-button')
    const leftGroup = sendButton.closest('.justify-between')
      ?.firstElementChild as HTMLElement
    // 窄栏（右面板展开压缩中栏）时左组内按钮换行，不与发送按钮/彼此叠压
    expect(leftGroup.className).toContain('flex-wrap')
  })

  it('上下文用量指示器允许收缩（min-w-0），不占死宽度', () => {
    renderInput()
    const indicator = screen.getByTestId('context-usage-indicator')
    expect(indicator.className).toContain('min-w-0')
  })

  it('发送消息携带思考选择（参数组 JSON 串透传，不带 enableThinking）', () => {
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
        thinkingStrength: 'high',
      }),
    )
    // enableThinking 业务链已退役：思考覆盖只走参数组 thinkingStrength 通道
    expect(onSendMessage.mock.calls[0][0]).not.toHaveProperty('enableThinking')
  })

  it('未选择（空串）→ 消息不带思考覆盖（thinkingStrength 随帧为空串）', () => {
    const onSendMessage = vi.fn()
    render(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage}
        modelName="deepseek-v3"
      />,
    )

    fireEvent.change(screen.getByTestId('chat-input-textarea'), { target: { value: 'hi' } })
    fireEvent.click(screen.getByTestId('chat-send-button'))

    expect(onSendMessage).toHaveBeenCalledWith(
      expect.objectContaining({ thinkingStrength: '' }),
    )
  })

  it('回车发送：Enter（无 Shift）触发发送；受理后输入保留（清空只在落定）', () => {
    const onSendMessage = vi.fn()
    render(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage}
        modelName="deepseek-v3"
        enableThinkingMode
        thinkingStrength="medium"
      />,
    )

    const textarea = screen.getByTestId('chat-input-textarea')
    fireEvent.change(textarea, { target: { value: '回车发送' } })
    fireEvent.keyDown(textarea, { key: 'Enter' })

    expect(onSendMessage).toHaveBeenCalledWith(
      expect.objectContaining({ content: '回车发送', thinkingStrength: 'medium' }),
    )
    // 2026-11-04 落定语义重定义：受理 = 发送态开始，清空由落定订阅回调按
    // 快照比对执行（见 ChatInput.sendLifecycle.test.tsx），此处输入保留
    expect((textarea as HTMLTextAreaElement).value).toBe('回车发送')
  })

  it('Shift+Enter 不发送（仅换行，onSendMessage 不被调用）', () => {
    const onSendMessage = vi.fn()
    render(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage}
        modelName="deepseek-v3"
        enableThinkingMode
        thinkingStrength="medium"
      />,
    )

    const textarea = screen.getByTestId('chat-input-textarea')
    fireEvent.change(textarea, { target: { value: '多行' } })
    fireEvent.keyDown(textarea, { key: 'Enter', shiftKey: true })

    expect(onSendMessage).not.toHaveBeenCalled()
    expect((textarea as HTMLTextAreaElement).value).toBe('多行')
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

  it('双态按钮：执行中且输入框有文字 → 发送按钮（可继续排队输入）', () => {
    const onSendMessage = vi.fn()
    const onStopGenerate = vi.fn()
    render(
      <ChatInput
        mode="full"
        isGenerating
        onSendMessage={onSendMessage}
        onStopGenerate={onStopGenerate}
        modelName="deepseek-v3"
      />,
    )

    const textarea = screen.getByTestId('chat-input-textarea')
    fireEvent.change(textarea, { target: { value: '排队消息' } })

    // 执行中 + 有文字：仍是发送按钮，点击入队发送
    expect(screen.queryByTestId('chat-stop-button')).toBeNull()
    const sendButton = screen.getByTestId('chat-send-button')
    expect(sendButton).not.toBeDisabled()
    fireEvent.click(sendButton)
    expect(onSendMessage).toHaveBeenCalledWith(expect.objectContaining({ content: '排队消息' }))
    expect(onStopGenerate).not.toHaveBeenCalled()
  })

  it('双态按钮：执行中且输入框为空 → 停止按钮（点击停止生成）', () => {
    const onSendMessage = vi.fn()
    const onStopGenerate = vi.fn()
    render(
      <ChatInput
        mode="full"
        isGenerating
        onSendMessage={onSendMessage}
        onStopGenerate={onStopGenerate}
        modelName="deepseek-v3"
      />,
    )

    // 执行中 + 无文字：停止按钮接管
    expect(screen.queryByTestId('chat-send-button')).toBeNull()
    const stopButton = screen.getByTestId('chat-stop-button')
    fireEvent.click(stopButton)
    expect(onStopGenerate).toHaveBeenCalledTimes(1)
    expect(onSendMessage).not.toHaveBeenCalled()
  })

  it('双态按钮：执行中无文字时输入文字 → 切回发送按钮', () => {
    const onStopGenerate = vi.fn()
    render(
      <ChatInput
        mode="full"
        isGenerating
        onSendMessage={() => {}}
        onStopGenerate={onStopGenerate}
        modelName="deepseek-v3"
      />,
    )

    expect(screen.getByTestId('chat-stop-button')).toBeInTheDocument()
    fireEvent.change(screen.getByTestId('chat-input-textarea'), { target: { value: 'x' } })
    expect(screen.queryByTestId('chat-stop-button')).toBeNull()
    expect(screen.getByTestId('chat-send-button')).toBeInTheDocument()
  })

  it('双态按钮：非执行中空输入 → 发送按钮禁用（不显示停止）', () => {
    render(
      <ChatInput
        mode="full"
        onSendMessage={() => {}}
        onStopGenerate={() => {}}
        modelName="deepseek-v3"
      />,
    )

    expect(screen.queryByTestId('chat-stop-button')).toBeNull()
    expect(screen.getByTestId('chat-send-button')).toBeDisabled()
  })
})

describe('ChatInput 引用卡片行挂载位置（R6：输入框容器内顶部）', () => {
  beforeEach(() => {
    useChatInputStore.setState({ citationsByDraftKey: {}, drafts: {} })
  })

  it('卡片行与 textarea 同容器（输入框本体），且位于 textarea 之前', () => {
    type AddInput = Parameters<ReturnType<typeof useChatInputStore.getState>['addCitation']>[1]
    useChatInputStore.getState().addCitation('layout-cit-k', {
      kind: 'message',
      title: '用户',
      excerpt: '被引用文字',
      createdAt: 1710000000000,
    } as AddInput)

    render(
      <ChatInput
        mode="full"
        onSendMessage={() => {}}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        draftKey="layout-cit-k"
      />,
    )

    const row = screen.getByTestId('citation-card-row')
    const textarea = screen.getByTestId('chat-input-textarea')
    // 祖先关系：卡片行挂在输入框容器（bordered 容器，textarea+底部工具条一体）内部
    const box = textarea.closest('[data-testid="chat-input-box"]')
    expect(box).not.toBeNull()
    expect((box as HTMLElement).contains(row)).toBe(true)
    // 顺序：卡片行在 textarea 之前（容器内顶部，邮件客户端收件人 chips 形态）
    expect(
      row.compareDocumentPosition(textarea) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })
})
