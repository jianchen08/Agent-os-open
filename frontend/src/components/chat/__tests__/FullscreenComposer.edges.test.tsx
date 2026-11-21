// @feature FP-0.2.评审引用交互 件一：全屏简化 composer 浮窗（边界补测） | @ci: frontend-test
/**
 * 全屏简化 composer 浮窗边界行为测试（补测）：
 * - 无草稿键（未选中会话/标签）时浮窗降级为空输入面，不写草稿；
 * - 卸载丢弃未消费的远程发送请求（陈旧请求不得在下次挂载自发发送）；
 * - 发送终局反馈：timeout 文案、failed 无原因兜底、landed 轻反馈自动消退；
 * - 仅消费本草稿键的新终局（历史终局不重放、他键终局不串显）；
 * - 拒绝反馈显示与下一次发送清除；
 * - 全屏期间真实输入框跳过高度重算（display:none 下 scrollHeight=0 会塌陷）。
 *
 * 断行为不断实现：断言全部落在用户可见面/可观察状态（草稿、按钮禁用、
 * 提示文案、textarea 高度是否被重算），不触碰内部 ref 与实现细节。
 */

import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
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

// 引用镜像行：真实实现会拉取引用面（本机无内核 → 网络重试日志干扰终局反馈
// 用例的自动消退计时）。本文件用例不覆盖引用面，桩掉外部依赖。
vi.mock('../ReferenceSelectionRow', () => ({
  ReferenceSelectionRow: () => null,
}))

// 声明式 widget 层：挂载即拉取声明数据（同上，本机无内核 → 网络重试）。
vi.mock('@/components/schema/DeclaredWidgetLayer', () => ({
  DeclaredWidgetLayer: () => null,
}))

const DRAFT_KEY = 'sess-fullscreen-edges'
const PIPELINE_ID = 'pipe-fullscreen-edges'

function enterFullscreen() {
  act(() => {
    useUIStore.setState({ workspaceFullscreen: true })
  })
}

const composerTextarea = () =>
  screen.getByTestId('fullscreen-composer-textarea') as HTMLTextAreaElement

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

beforeEach(() => {
  localStorage.clear()
  useUIStore.setState({ workspaceFullscreen: false })
  useSessionStore.setState({ activeSessionId: DRAFT_KEY })
  useAgentTabStore.setState({ activeTabId: null, tabs: [] })
  useChatInputStore.setState({
    pendingInsert: null,
    pendingRemoteSend: false,
    remoteSendRefusal: null,
    drafts: {},
    sendInFlightByPipeline: {},
    sendOutcomes: [],
  })
})

describe('FullscreenComposer 无草稿键降级', () => {
  it('未选中会话/标签（draftKey 缺省）→ 浮窗仍可渲染，输入不落草稿', () => {
    useSessionStore.setState({ activeSessionId: null })
    useAgentTabStore.setState({ activeTabId: null, tabs: [] })
    enterFullscreen()
    render(<FullscreenComposer />)

    fireEvent.change(composerTextarea(), { target: { value: '无键输入' } })

    expect(useChatInputStore.getState().drafts).toEqual({})
    // 受控值只读草稿真源：无键 → 始终空（不假装有本地真源）
    expect(composerTextarea().value).toBe('')
  })
})

describe('FullscreenComposer 卸载语义', () => {
  it('卸载丢弃未消费的远程发送请求（陈旧请求不得在下次挂载自发发送）', () => {
    enterFullscreen()
    const { unmount } = render(<FullscreenComposer />)

    act(() => {
      useChatInputStore.getState().requestRemoteSend()
    })
    expect(useChatInputStore.getState().pendingRemoteSend).toBe(true)

    unmount()
    expect(useChatInputStore.getState().pendingRemoteSend).toBe(false)
  })
})

describe('FullscreenComposer 终局反馈', () => {
  it('timeout 终局 → 显示超时文案（与主输入框操作点口径一致）', () => {
    enterFullscreen()
    render(<FullscreenComposer />)

    act(() => {
      useChatInputStore.setState({
        sendOutcomes: [
          {
            id: 1,
            clientMessageId: 'cmid-timeout',
            pipelineId: PIPELINE_ID,
            kind: 'timeout',
            content: '等超时',
            draftKey: DRAFT_KEY,
          },
        ],
      })
    })

    expect(screen.getByTestId('fullscreen-composer-error')).toHaveTextContent(
      '发送超时，未收到送达确认——请重试',
    )
  })

  it('failed 终局无原因 → 显示兜底文案（不静默）', () => {
    enterFullscreen()
    render(<FullscreenComposer />)

    act(() => {
      useChatInputStore.setState({
        sendOutcomes: [
          {
            id: 2,
            clientMessageId: 'cmid-failed-noreason',
            pipelineId: PIPELINE_ID,
            kind: 'failed',
            content: '等失败',
            draftKey: DRAFT_KEY,
          },
        ],
      })
    })

    expect(screen.getByTestId('fullscreen-composer-error')).toHaveTextContent(
      '消息发送失败，请重新发送',
    )
  })

  it('landed 终局 → 「已发送」轻反馈，自动消退后提示消失', async () => {
    enterFullscreen()
    render(<FullscreenComposer />)

    act(() => {
      useChatInputStore.setState({
        sendOutcomes: [
          {
            id: 3,
            clientMessageId: 'cmid-landed',
            pipelineId: PIPELINE_ID,
            kind: 'landed',
            content: '已发',
            draftKey: DRAFT_KEY,
          },
        ],
      })
    })
    expect(screen.getByTestId('fullscreen-composer-feedback')).toHaveTextContent('已发送')

    // 轻反馈自动消退（LANDED_FEEDBACK_MS=2500）：真实计时器等待，避免假时钟
    // 与 React 调度交错（本套件其余用例均走真实计时器）
    await waitFor(
      () => expect(screen.queryByTestId('fullscreen-composer-feedback')).toBeNull(),
      { timeout: 4000 },
    )
  })

  it('历史终局不重放：挂载前已存在的终局不产生提示', () => {
    act(() => {
      useChatInputStore.setState({
        sendOutcomes: [
          {
            id: 9,
            clientMessageId: 'cmid-history',
            pipelineId: PIPELINE_ID,
            kind: 'failed',
            content: '历史',
            draftKey: DRAFT_KEY,
            reason: '历史失败',
          },
        ],
      })
    })
    enterFullscreen()
    render(<FullscreenComposer />)

    expect(screen.queryByTestId('fullscreen-composer-error')).toBeNull()
  })

  it('他草稿键的终局不串显到本浮窗', () => {
    enterFullscreen()
    render(<FullscreenComposer />)

    act(() => {
      useChatInputStore.setState({
        sendOutcomes: [
          {
            id: 4,
            clientMessageId: 'cmid-other',
            pipelineId: PIPELINE_ID,
            kind: 'failed',
            content: '别处',
            draftKey: 'other-draft-key',
            reason: '别的会话失败',
          },
        ],
      })
    })

    expect(screen.queryByTestId('fullscreen-composer-error')).toBeNull()
  })
})

describe('FullscreenComposer 拒绝反馈', () => {
  it('拒绝原因可见；下一次发送清除（旧反馈不残留）', () => {
    enterFullscreen()
    const onSendMessage = vi.fn(() => true)
    renderBoth(onSendMessage)

    act(() => {
      useChatInputStore.getState().reportRemoteSendRefusal('会话执行中，暂不能发送')
    })
    expect(screen.getByTestId('fullscreen-composer-error')).toHaveTextContent(
      '会话执行中，暂不能发送',
    )

    // 空草稿发送：浮窗先清旧反馈，再触发远程发送（主输入框 handleSend 无内容拒绝）
    fireEvent.click(screen.getByTestId('fullscreen-composer-send'))
    expect(useChatInputStore.getState().pendingRemoteSend).toBe(false)
    expect(onSendMessage).not.toHaveBeenCalled()
    expect(screen.queryByTestId('fullscreen-composer-error')).toBeNull()
  })
})

describe('全屏期间真实输入框高度重算跳过', () => {
  it('全屏下编辑不重算高度（display:none 时 scrollHeight=0 会塌陷），退出后恢复', () => {
    enterFullscreen()
    renderBoth(vi.fn(() => true))
    const textarea = screen.getByTestId('chat-input-textarea') as HTMLTextAreaElement

    // 挂载 + 全屏期间编辑：高度一律不动（守卫生效）
    expect(textarea.style.height).toBe('')
    fireEvent.change(textarea, { target: { value: '全屏期间输入一段' } })
    expect(textarea.style.height).toBe('')
    expect(useChatInputStore.getState().drafts[DRAFT_KEY]).toBe('全屏期间输入一段')

    // 退出全屏后同一输入框恢复高度重算（对照组：有区分度）
    act(() => {
      useUIStore.setState({ workspaceFullscreen: false })
    })
    fireEvent.change(textarea, { target: { value: '退出全屏后输入一段' } })
    expect(textarea.style.height).not.toBe('')
  })
})

describe('ChatInput 草稿 store→本地镜像', () => {
  it('store 草稿被外部写入 → 真实输入框正文同步（同源同内容）', () => {
    renderBoth(vi.fn(() => true))
    const textarea = screen.getByTestId('chat-input-textarea') as HTMLTextAreaElement

    act(() => {
      useChatInputStore.getState().saveDraft(DRAFT_KEY, 'store 侧新内容')
    })
    expect(textarea.value).toBe('store 侧新内容')
  })
})
