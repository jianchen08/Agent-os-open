/** @feature FP-0.2.待发送队列 发送态生命周期（落定语义） | @ci: frontend-test
 * 发送态生命周期重定义（落定语义）ChatInput 行为测试
 *
 * 用户裁定（2026-11-04）：
 *   - 受理成功 ≠ 清空：文本/草稿保留到落定（气泡上屏/队列条出现）那一刻；
 *   - 发送态期间 textarea 锁定不可编辑、不可再次发送；
 *   - 5s 超时 → 判死：解锁 + 操作点报错（error 级；停止链归宿主层订阅驱动）；
 *   - 判死后迟到落定不触碰输入（消息流以气泡为准，文本归属用户重试）。
 */
import { act, fireEvent, screen } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
const getModelsMock = vi.fn()
vi.mock('@/services/api/config', () => ({
  getModels: (...args: unknown[]) => getModelsMock(...args),
}))
getModelsMock.mockImplementation(() => new Promise(() => {}))
const fetchStatesMock = vi.fn()
vi.mock('@/services/api/pipelines', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  fetchPipelineStates: (...args: unknown[]) => fetchStatesMock(...args),
}))
fetchStatesMock.mockImplementation(() => new Promise(() => {}))
import { useChatInputStore } from '@/stores/chatInputStore'
import { renderWithProviders as render } from '@/test/renderWithProviders'
import { composeCitationEntry } from '@/utils/citationCompose'
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

const DRAFT_KEY = 'send-lifecycle-draft'
const PIPELINE_ID = 'pipe-lifecycle-1'

/** 模拟 router 受理路径：生成 cmid、beginSend 登记快照、返回受理 */
function acceptedSender() {
  return vi.fn((params: { composerText?: string }) => {
    useChatInputStore.getState().beginSend(
      PIPELINE_ID,
      `cmid-${useChatInputStore.getState().sendOutcomes.length + 1}`,
      params.composerText ?? '',
      DRAFT_KEY,
    )
    return true
  })
}

function renderInput(onSendMessage: (params: unknown) => boolean | void, sendInFlight = false) {
  return render(
    <ChatInput
      mode="full"
      onSendMessage={onSendMessage as never}
      enableFileUpload
      enableDragDrop
      modelName="deepseek-v3"
      draftKey={DRAFT_KEY}
      sendInFlight={sendInFlight}
    />,
  )
}

const textarea = () => screen.getByTestId('chat-input-textarea') as HTMLTextAreaElement
const textareaValue = () => textarea().value

function typeAndSend(text: string) {
  fireEvent.change(textarea(), { target: { value: text } })
  fireEvent.click(screen.getByTestId('chat-send-button'))
}

describe('ChatInput 发送态生命周期（落定语义）', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-04T00:00:00Z'))
    useChatInputStore.setState({
      pendingInsert: null,
      drafts: {},
      sendInFlightByPipeline: {},
      sendOutcomes: [],
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('受理成功后文本与草稿保留（清空只在落定）', () => {
    const onSendMessage = acceptedSender()
    renderInput(onSendMessage)

    typeAndSend('这条消息还在路上')

    expect(onSendMessage).toHaveBeenCalledTimes(1)
    expect(textareaValue()).toBe('这条消息还在路上')
    expect(useChatInputStore.getState().drafts[DRAFT_KEY]).toBe('这条消息还在路上')
  })

  it('发送在途锁定输入：textarea disabled、占位「发送中…」、按钮转圈不可再发', () => {
    const onSendMessage = acceptedSender()
    const { rerender } = renderInput(onSendMessage)
    typeAndSend('锁定中')
    // 父层（ChatContainer）从 store 派生 sendInFlight=true
    rerender(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        draftKey={DRAFT_KEY}
        sendInFlight
      />,
    )

    expect(textarea()).toBeDisabled()
    expect(textarea().placeholder).toContain('发送中')
    expect(screen.getByTestId('chat-send-inflight')).toBeDisabled()
    expect(screen.queryByTestId('chat-send-button')).toBeNull()
  })

  it('落定后清空文本+草稿并撤锁（气泡上屏那一刻才算成功）', () => {
    const onSendMessage = acceptedSender()
    const { rerender } = renderInput(onSendMessage)
    typeAndSend('落定即清空')
    rerender(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        draftKey={DRAFT_KEY}
        sendInFlight
      />,
    )

    // 认领/队列条到达 → 按受理序落定（此处首条 = cmid-1）
    act(() => {
      useChatInputStore.getState().landSendByCmid('cmid-1')
    })
    rerender(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        draftKey={DRAFT_KEY}
        sendInFlight={false}
      />,
    )

    expect(textareaValue()).toBe('')
    expect(useChatInputStore.getState().drafts[DRAFT_KEY]).toBeUndefined()
    expect(textarea()).not.toBeDisabled()
  })

  it('5s 超时判死：解锁 + 操作点报错（error 级），文本保留', () => {
    const onSendMessage = acceptedSender()
    const { rerender } = renderInput(onSendMessage)
    typeAndSend('慢消息')
    rerender(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        draftKey={DRAFT_KEY}
        sendInFlight
      />,
    )

    act(() => {
      vi.advanceTimersByTime(5000)
    })
    rerender(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        draftKey={DRAFT_KEY}
        sendInFlight={false}
      />,
    )

    // 解锁 + 文本保留 + 判死报错落在操作点（error alert 语义）
    expect(textarea()).not.toBeDisabled()
    expect(textareaValue()).toBe('慢消息')
    expect(screen.getByRole('alert')).toHaveTextContent('发送超时，已停止执行')
    // 判死统一 error 语义：不再有非 error 级警告条
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('判死后迟到落定不触碰输入：文本未被判死前的落定吞掉；未判死的落定照常清空', () => {
    const onSendMessage = acceptedSender()
    const { rerender } = renderInput(onSendMessage)
    typeAndSend('原始文本')
    rerender(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        draftKey={DRAFT_KEY}
        sendInFlight
      />,
    )

    // 5s 超时 → 判死（终局）
    act(() => {
      vi.advanceTimersByTime(5000)
    })
    rerender(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        draftKey={DRAFT_KEY}
        sendInFlight={false}
      />,
    )

    // 迟到 confirmed（气泡上屏）：输入框文本不动（消息已判死，归属用户重试）
    act(() => {
      useChatInputStore.getState().landSendByCmid('cmid-1')
    })
    expect(textareaValue()).toBe('原始文本')

    // 对照组：未判死的正常落定仍照常清空（消息已上屏不重复）
    act(() => {
      useChatInputStore.getState().beginSend(PIPELINE_ID, 'cmid-2', '原始文本', DRAFT_KEY)
    })
    act(() => {
      useChatInputStore.getState().landSendByCmid('cmid-2')
    })
    expect(textareaValue()).toBe('')
  })

  it('失败判死（20s 看门狗/ack 拒收）：操作点报错，文本保留可重试', () => {
    const onSendMessage = acceptedSender()
    const { rerender } = renderInput(onSendMessage)
    typeAndSend('会被拒收的消息')
    rerender(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        draftKey={DRAFT_KEY}
        sendInFlight
      />,
    )

    act(() => {
      useChatInputStore.getState().endSendByCmid('cmid-1', '服务器拒绝派发：会话无可派发管道')
    })
    rerender(
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        draftKey={DRAFT_KEY}
        sendInFlight={false}
      />,
    )

    // 判死落在操作点（error alert），文本从未离开输入框（天然可重试）
    expect(screen.getByRole('alert')).toHaveTextContent('消息发送失败：服务器拒绝派发：会话无可派发管道')
    expect(textareaValue()).toBe('会被拒收的消息')
    expect(textarea()).not.toBeDisabled()
    // 警告条不出现（failed ≠ timeout）
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('发送态期间 handleSend 兜底拒绝（textarea disabled 后键盘路径不可达性防御）', () => {
    const onSendMessage = vi.fn(() => true)
    renderInput(onSendMessage, true)
    // jsdom 下 fireEvent 可向 disabled textarea 派发事件（真实浏览器不可达），
    // 守卫必须保证发送态不产生第二次受理
    fireEvent.change(textarea(), { target: { value: '锁定期输入' } })
    fireEvent.keyDown(textarea(), { key: 'Enter' })
    expect(onSendMessage).not.toHaveBeenCalled()
  })

  it('无草稿键的发送：按快照文本归属，落定清空', () => {
    // 语音/外部插入等不带 draftKey 的登记路径：outcome.content == 当前文本才算本输入器
    const { rerender } = render(
      <ChatInput
        mode="full"
        onSendMessage={vi.fn() as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
      />,
    )
    fireEvent.change(textarea(), { target: { value: '无草稿键消息' } })
    act(() => {
      useChatInputStore.getState().beginSend(PIPELINE_ID, 'cmid-nodraft', '无草稿键消息')
    })
    rerender(
      <ChatInput
        mode="full"
        onSendMessage={vi.fn() as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        sendInFlight
      />,
    )
    act(() => {
      useChatInputStore.getState().landSendByCmid('cmid-nodraft')
    })
    rerender(
      <ChatInput
        mode="full"
        onSendMessage={vi.fn() as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        sendInFlight={false}
      />,
    )
    expect(textareaValue()).toBe('')
  })
})

describe('ChatInput 引用条目发送生命周期（R5 卡片通道：落定清列、失败保留）', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-11-04T00:00:00Z'))
    useChatInputStore.setState({
      pendingInsert: null,
      drafts: {},
      citationsByDraftKey: {},
      sendInFlightByPipeline: {},
      sendOutcomes: [],
    })
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('发送时卡片条目按序并入正文（content 含块引用），落定清列+清文本', () => {
    const TS = '2026-11-05T06:30:00Z'
    useChatInputStore.getState().addCitation(DRAFT_KEY, {
      id: 'cit-1',
      kind: 'message',
      title: '用户',
      excerpt: '被引用段落',
      createdAt: Date.parse(TS),
    })
    const onSendMessage = acceptedSender()
    renderInput(onSendMessage)

    typeAndSend('看这个')
    expect(onSendMessage).toHaveBeenCalledTimes(1)
    const params = onSendMessage.mock.calls[0][0] as { content: string }
    // 期望=旧通道真源组装（requestInsert 插入的正是该字符串）+ 空行分隔
    const oldChannelBlock = composeCitationEntry({
      content: '被引用段落',
      sourceLabel: '用户',
      timestamp: TS,
    })
    expect(params.content).toBe(`看这个\n\n${oldChannelBlock}`)

    act(() => {
      useChatInputStore.getState().landSendByCmid('cmid-1')
    })
    expect(useChatInputStore.getState().citationsByDraftKey[DRAFT_KEY]).toBeUndefined()
    expect(textareaValue()).toBe('')
  })

  it('失败判死：条目保留（与正文草稿同语义，可重试）', () => {
    useChatInputStore.getState().addCitation(DRAFT_KEY, {
      id: 'cit-2',
      kind: 'code',
      title: 'src/main.py:L2',
      excerpt: 'L2: line2',
      comment: '复核',
    })
    const onSendMessage = acceptedSender()
    renderInput(onSendMessage)
    typeAndSend('会被拒收的消息')

    act(() => {
      useChatInputStore.getState().endSendByCmid('cmid-1', 'ack 拒收')
    })
    const remain = useChatInputStore.getState().citationsByDraftKey[DRAFT_KEY] ?? []
    expect(remain.map((c) => c.id)).toEqual(['cit-2'])
    expect(remain[0].comment).toBe('复核')
  })

  it('5s 超时判死：条目保留', () => {
    useChatInputStore.getState().addCitation(DRAFT_KEY, {
      id: 'cit-3',
      kind: 'message',
      title: '助手',
      excerpt: '超时条目',
    })
    const onSendMessage = acceptedSender()
    renderInput(onSendMessage)
    typeAndSend('慢消息')

    act(() => {
      vi.advanceTimersByTime(5000)
    })
    const remain = useChatInputStore.getState().citationsByDraftKey[DRAFT_KEY] ?? []
    expect(remain.map((c) => c.id)).toEqual(['cit-3'])
  })

  it('仅卡片条目无正文也可发送（旧通道引用文本即正文的等价能力）', () => {
    useChatInputStore.getState().addCitation(DRAFT_KEY, {
      id: 'cit-4',
      kind: 'doc',
      title: 'docs/设计.md:L3',
      excerpt: '章节内容',
    })
    const onSendMessage = acceptedSender()
    renderInput(onSendMessage)

    fireEvent.click(screen.getByTestId('chat-send-button'))
    expect(onSendMessage).toHaveBeenCalledTimes(1)
    const params = onSendMessage.mock.calls[0][0] as { content: string }
    expect(params.content).toBe('> 【docs/设计.md:L3】\n> 章节内容')
  })
})
