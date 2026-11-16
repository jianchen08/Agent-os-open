// @feature FP-0.2.评审引用交互 件一：全屏简化 composer 浮窗 | @ci: frontend-test
/**
 * 全屏简化 composer 浮窗行为测试（2026-11-05 阶段一件一）。
 *
 * 设计真值 = docs/working/调研_评审引用交互与默认模型显示_20261104.md §6.1-3
 * （用户拍板：与真实输入框同源同内容，仅显示简化）。
 *
 * 同参发送的到达语义：浮窗只触发（store requestRemoteSend 置位），隐藏但
 * 保挂载的真实 ChatInput 订阅消费并调用自身 handleSend——附件/modelId 等参数
 * 状态是 ChatInput 局部，浮窗不拼参（禁复制发送逻辑）。
 *
 * 外部依赖 mock：ChatInput 的重组件子件（与 ChatInput.sendLifecycle 同型）；
 * chatInputStore/uiStore/sessionStore/agentTabStore 走真实模块（内存态）。
 */

import { act, fireEvent, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { registerReferenceProvider } from '@/services/references'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useChatInputStore } from '@/stores/chatInputStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useUIStore } from '@/stores/uiStore'
import { renderWithProviders as render } from '@/test/renderWithProviders'
import { ChatInput } from '../ChatInput'
import { FullscreenComposer } from '../FullscreenComposer'

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

const DRAFT_KEY = 'sess-fullscreen-1'
const PIPELINE_ID = 'pipe-fullscreen-1'

const consumeMock = vi.fn()
/** 待发引用 provider 桩：发送装配期 buildReferenceBlock 取其 selection */
function registerStubReferenceProvider(withSelection = true) {
  registerReferenceProvider({
    source: 'test',
    getSelection: () =>
      withSelection
        ? {
            source: 'test',
            items: [{ name: '场景A', type: 'scene', path: 'res://a.tscn' }],
          }
        : null,
    consume: consumeMock,
  })
}

/** ChatInput（模拟全屏下隐藏保挂载的真实输入框）+ 浮窗同树渲染 */
function renderBoth(onSendMessage: (params: unknown) => boolean | void, chatInputProps = {}) {
  return render(
    <>
      <ChatInput
        mode="full"
        onSendMessage={onSendMessage as never}
        enableFileUpload
        enableDragDrop
        modelName="deepseek-v3"
        draftKey={DRAFT_KEY}
        {...chatInputProps}
      />
      <FullscreenComposer />
    </>,
  )
}

function enterFullscreen() {
  act(() => {
    useUIStore.setState({ workspaceFullscreen: true })
  })
}

const composerTextarea = () =>
  screen.getByTestId('fullscreen-composer-textarea') as HTMLTextAreaElement

beforeEach(() => {
  localStorage.clear()
  consumeMock.mockClear()
  useUIStore.setState({ workspaceFullscreen: false })
  useSessionStore.setState({ activeSessionId: DRAFT_KEY })
  useAgentTabStore.setState({ activeTabId: null, tabs: [] })
  useChatInputStore.setState({
    pendingInsert: null,
    pendingRemoteSend: false,
    remoteSendRefusal: null,
    drafts: {},
    citationsByDraftKey: {},
    sendInFlightByPipeline: {},
    sendOutcomes: [],
  })
})

describe('FullscreenComposer 渲染门控', () => {
  it('workspaceFullscreen=false → 不渲染浮窗', () => {
    render(<FullscreenComposer />)
    expect(screen.queryByTestId('fullscreen-composer')).toBeNull()
  })

  it('workspaceFullscreen=true → 渲染浮窗（文本区/引用镜像行挂点/发送/退出）', () => {
    enterFullscreen()
    render(<FullscreenComposer />)
    expect(screen.getByTestId('fullscreen-composer')).toBeInTheDocument()
    expect(screen.getByTestId('fullscreen-composer-textarea')).toBeInTheDocument()
    expect(screen.getByTestId('fullscreen-composer-send')).toBeInTheDocument()
    expect(screen.getByTestId('fullscreen-composer-exit')).toBeInTheDocument()
  })
})

describe('FullscreenComposer 草稿双向同步（同源同内容）', () => {
  it('浮窗编辑 → store 草稿同值（两组有区分度输入）', () => {
    enterFullscreen()
    render(<FullscreenComposer />)

    fireEvent.change(composerTextarea(), { target: { value: '浮窗输入甲' } })
    expect(useChatInputStore.getState().drafts[DRAFT_KEY]).toBe('浮窗输入甲')

    fireEvent.change(composerTextarea(), { target: { value: '浮窗输入乙-不同内容' } })
    expect(useChatInputStore.getState().drafts[DRAFT_KEY]).toBe('浮窗输入乙-不同内容')
  })

  it('store 侧草稿写入（模拟隐藏主输入框消费路径）→ 浮窗显示同值（两组）', () => {
    enterFullscreen()
    render(<FullscreenComposer />)

    act(() => {
      useChatInputStore.getState().saveDraft(DRAFT_KEY, 'store 侧写入第一组')
    })
    expect(composerTextarea().value).toBe('store 侧写入第一组')

    act(() => {
      useChatInputStore.getState().saveDraft(DRAFT_KEY, 'store 侧写入第二组')
    })
    expect(composerTextarea().value).toBe('store 侧写入第二组')
  })

  it('requestInsert 到达 → 隐藏 ChatInput 消费写草稿 → 浮窗实时显示', () => {
    enterFullscreen()
    renderBoth(vi.fn(() => true))

    act(() => {
      useChatInputStore.getState().requestInsert('> 引用 arrived A')
    })
    expect(composerTextarea().value).toBe('> 引用 arrived A')

    act(() => {
      useChatInputStore.getState().requestInsert('> 引用 arrived B')
    })
    expect(composerTextarea().value).toBe('> 引用 arrived A\n> 引用 arrived B')
  })
})

describe('FullscreenComposer 引用卡片行（R5 入列可见/R6 容器内挂载、同源移除）', () => {
  it('入列条目 → 浮窗内可见同列卡片，挂正文输入容器内（textarea 之前）+ 评语文本可见', () => {
    enterFullscreen()
    render(<FullscreenComposer />)

    act(() => {
      useChatInputStore.getState().addCitation(DRAFT_KEY, {
        id: 'fs-c1',
        kind: 'code',
        title: 'src/main.py:L2',
        excerpt: 'L2: line2',
      })
      useChatInputStore.getState().addCitation(DRAFT_KEY, {
        id: 'fs-c2',
        kind: 'message',
        title: '用户',
        excerpt: '被引用文字',
        comment: '复核',
      })
    })

    const cards = screen.getAllByTestId('citation-card')
    expect(cards.map((c) => c.getAttribute('data-card-id'))).toEqual(['fs-c1', 'fs-c2'])
    expect(screen.getByText('代码')).toBeInTheDocument()
    expect(screen.getByText('消息')).toBeInTheDocument()
    // 评语文本直接可见（R6 后置渲染，非标记徽章）
    expect(screen.getByTestId('citation-card-comment')).toHaveTextContent('复核')

    // 挂载位置（R6）：卡片行挂正文输入容器（textarea 的 bordered 容器）内，且在 textarea 之前
    const row = screen.getByTestId('citation-card-row')
    const textarea = screen.getByTestId('fullscreen-composer-textarea')
    const box = textarea.closest('[data-testid="fullscreen-composer-input-box"]')
    expect(box).not.toBeNull()
    expect((box as HTMLElement).contains(row)).toBe(true)
    expect(
      row.compareDocumentPosition(textarea) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy()
  })

  it('浮窗内 × 移除 → 同源 store 列更新（主输入框同列同步）', () => {
    enterFullscreen()
    render(<FullscreenComposer />)

    act(() => {
      useChatInputStore.getState().addCitation(DRAFT_KEY, {
        id: 'fs-del',
        kind: 'doc',
        title: 'docs/a.md',
        excerpt: '内容',
      })
    })
    expect(screen.getAllByTestId('citation-card')).toHaveLength(1)

    fireEvent.click(screen.getByTestId('citation-card-remove'))

    expect(useChatInputStore.getState().citationsByDraftKey[DRAFT_KEY]).toBeUndefined()
    expect(screen.queryByTestId('citation-card')).not.toBeInTheDocument()
  })

  it('发送落定 → 卡片列清空（与主输入框同源清理）', () => {
    const onSendMessage = vi.fn((params: { composerText?: string }) => {
      useChatInputStore
        .getState()
        .beginSend(PIPELINE_ID, 'cmid-fs-cit', params.composerText ?? '', DRAFT_KEY)
      return true
    })
    enterFullscreen()
    renderBoth(onSendMessage)

    act(() => {
      useChatInputStore.getState().addCitation(DRAFT_KEY, {
        id: 'fs-send',
        kind: 'message',
        title: '用户',
        excerpt: '随发送条目',
      })
    })
    fireEvent.change(composerTextarea(), { target: { value: '带引用发送' } })
    fireEvent.click(screen.getByTestId('fullscreen-composer-send'))

    act(() => {
      useChatInputStore.getState().landSendByCmid('cmid-fs-cit')
    })
    expect(useChatInputStore.getState().citationsByDraftKey[DRAFT_KEY]).toBeUndefined()
    expect(screen.queryByTestId('citation-card')).not.toBeInTheDocument()
  })
})

describe('FullscreenComposer 正文 auto-grow（R4 件①）', () => {
  it('输入高度随内容重算：短文本不封顶、长文本封顶 max-h-40（160px）', () => {
    enterFullscreen()
    render(<FullscreenComposer />)
    const el = composerTextarea()
    // jsdom 无布局：桩定 scrollHeight 模拟本征内容高（断言换算行为非布局）
    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 40 })
    fireEvent.change(el, { target: { value: '短句' } })
    expect(el.style.height).toBe('40px')

    Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => 400 })
    fireEvent.change(el, { target: { value: '长文本'.repeat(200) } })
    expect(el.style.height).toBe('160px')
  })
})

describe('FullscreenComposer 发送与主输入框同参', () => {
  it('浮窗发送经真实 ChatInput handleSend：正文含草稿、引用块合并在内、modelId 透传', () => {
    registerStubReferenceProvider(true)
    const onSendMessage = vi.fn(() => true)
    enterFullscreen()
    renderBoth(onSendMessage, { modelId: 'model-x' })

    fireEvent.change(composerTextarea(), { target: { value: '评审这条' } })
    fireEvent.click(screen.getByTestId('fullscreen-composer-send'))

    expect(onSendMessage).toHaveBeenCalledTimes(1)
    const params = onSendMessage.mock.calls[0][0] as {
      content: string
      modelId?: string
      composerText?: string
    }
    expect(params.content).toContain('评审这条')
    expect(params.content).toContain('<reference source="test"')
    expect(params.modelId).toBe('model-x')
    expect(params.composerText).toBe('评审这条')
    // 引用消费与主输入框同路径（composeSendParams 消费方在 ChatInput.handleSend）
    expect(consumeMock).toHaveBeenCalledTimes(1)
  })

  it('落定清空与主输入框一致：landed 后浮窗与草稿同步清空', () => {
    const onSendMessage = vi.fn((params: { composerText?: string }) => {
      useChatInputStore
        .getState()
        .beginSend(PIPELINE_ID, 'cmid-fs-1', params.composerText ?? '', DRAFT_KEY)
      return true
    })
    enterFullscreen()
    renderBoth(onSendMessage)

    fireEvent.change(composerTextarea(), { target: { value: '等落定' } })
    fireEvent.click(screen.getByTestId('fullscreen-composer-send'))
    expect(onSendMessage).toHaveBeenCalledTimes(1)

    act(() => {
      useChatInputStore.getState().landSendByCmid('cmid-fs-1')
    })
    expect(useChatInputStore.getState().drafts[DRAFT_KEY]).toBeUndefined()
    expect(composerTextarea().value).toBe('')
  })
})

describe('FullscreenComposer 反馈与退出', () => {
  it('主输入框 disabled → 浮窗发送显示拒绝原因（不静默）', () => {
    enterFullscreen()
    const onSendMessage = vi.fn(() => true)
    renderBoth(onSendMessage, { disabled: true })

    fireEvent.change(composerTextarea(), { target: { value: '被拒文本' } })
    fireEvent.click(screen.getByTestId('fullscreen-composer-send'))

    expect(onSendMessage).not.toHaveBeenCalled()
    expect(screen.getByTestId('fullscreen-composer-error')).toHaveTextContent(
      '会话执行中，暂不能发送',
    )
  })

  it('发送终局 failed → 浮窗显示失败原因（与主输入框错误文案一致）', () => {
    enterFullscreen()
    renderBoth(vi.fn(() => true))

    act(() => {
      useChatInputStore.setState({
        sendOutcomes: [
          {
            id: 1,
            clientMessageId: 'cmid-x',
            pipelineId: PIPELINE_ID,
            kind: 'failed',
            content: '',
            draftKey: DRAFT_KEY,
            reason: 'ack 拒收',
          },
        ],
      })
    })
    expect(screen.getByTestId('fullscreen-composer-error')).toHaveTextContent('ack 拒收')
  })

  it('sendInFlight（本管道在途）→ 浮窗发送按钮禁用', () => {
    enterFullscreen()
    useAgentTabStore.setState({
      activeTabId: 'tab-1',
      tabs: [
        {
          id: 'tab-1',
          agentId: 'a',
          agentName: 'A',
          agentLevel: 1,
          path: [],
          status: 'running' as const,
          hasUnread: false,
          canClose: true,
          pipelineRunId: PIPELINE_ID,
        },
      ],
    })
    useChatInputStore.setState({
      sendInFlightByPipeline: {
        [PIPELINE_ID]: [
          { clientMessageId: 'c1', content: '', startedAt: Date.now() },
        ],
      },
    })
    renderBoth(vi.fn(() => true))

    expect(screen.getByTestId('fullscreen-composer-send')).toBeDisabled()
  })

  it('退出全屏按钮 → workspaceFullscreen=false', () => {
    enterFullscreen()
    render(<FullscreenComposer />)
    fireEvent.click(screen.getByTestId('fullscreen-composer-exit'))
    expect(useUIStore.getState().workspaceFullscreen).toBe(false)
  })
})

describe('浮窗限高限宽与右下角定位（R7 Bug②：长卡片不顶出屏幕）', () => {
  it('定位=右下角（bottom-4 right-4），不再水平居中（无 left-1/2 / -translate-x-1/2）', () => {
    enterFullscreen()
    render(<FullscreenComposer />)
    const root = screen.getByTestId('fullscreen-composer')
    expect(root.className).toContain('bottom-4')
    expect(root.className).toContain('right-4')
    expect(root.className).not.toContain('left-1/2')
    expect(root.className).not.toContain('-translate-x-1/2')
  })

  it('容器限高 max-h-[60vh]（flex-col）；发送/退出按钮在滚动容器外（恒可见不滚走）', () => {
    enterFullscreen()
    render(<FullscreenComposer />)
    const root = screen.getByTestId('fullscreen-composer')
    expect(root.className).toContain('max-h-[60vh]')
    expect(root.className).toContain('flex-col')

    const send = screen.getByTestId('fullscreen-composer-send')
    const exit = screen.getByTestId('fullscreen-composer-exit')
    expect(send.closest('.overflow-y-auto')).toBeNull()
    expect(exit.closest('.overflow-y-auto')).toBeNull()
    expect(send.className).toContain('shrink-0')
    expect(exit.className).toContain('shrink-0')
  })

  it('浮窗实例卡片行自身封高（max-h-32）滚动：长卡片行不撑破容器', () => {
    enterFullscreen()
    render(<FullscreenComposer />)
    act(() => {
      for (let i = 0; i < 6; i++) {
        useChatInputStore.getState().addCitation(DRAFT_KEY, {
          id: `fs-tall-${i}`,
          kind: 'message',
          title: '用户',
          excerpt: `第${i}条摘录内容`,
        })
      }
    })

    const row = screen.getByTestId('citation-card-row')
    expect(row.className).toContain('max-h-32')
    expect(row.className).toContain('overflow-y-auto')
    expect(screen.getAllByTestId('citation-card')).toHaveLength(6)
  })

  it('窄容器横向防撑破：输入容器 min-w-0（flex 子项可收缩），摘录/评语 truncate 在位', () => {
    enterFullscreen()
    render(<FullscreenComposer />)
    act(() => {
      useChatInputStore.getState().addCitation(DRAFT_KEY, {
        id: 'fs-wide',
        kind: 'code',
        title: 'a/very/long/path/that/keeps/going/main.py:L2',
        excerpt: 'x'.repeat(200),
        comment: '一条相当长的评审意见也会在窄容器下收缩'.repeat(3),
      })
    })

    const box = screen.getByTestId('fullscreen-composer-input-box')
    expect(box.className).toContain('min-w-0')
    const card = screen.getByTestId('citation-card')
    expect(card.className).toContain('min-w-0')
    expect(screen.getByTestId('citation-card-excerpt').className).toContain('truncate')
    expect(screen.getByTestId('citation-card-comment').className).toContain('truncate')
  })
})
