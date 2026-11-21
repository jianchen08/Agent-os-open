// @feature FP-0.2.mobile_visual_p1 @ci: frontend-test
/**
 * 作曲器模型信息单入口（P1-1，MuMu 实测 shots/04 三处冗余 + 发送键错位）：
 * - 移动视口：思考强度/模型两槽位不再内联平铺（底部整行模型名消失），单入口
 *   chip 在场；点开面板后两槽位渲染层可达；
 * - 桌面视口：两槽位内联平铺契约不变（单入口退场）；
 * - 发送键区与左组顶行对齐（items-start，换行时不再垂直悬中）。
 */
import { act, fireEvent, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ChatInput } from '@/components/chat/ChatInput'
import { renderWithProviders as render } from '@/test/renderWithProviders'

const mocks = vi.hoisted(() => ({
  caps: {
    canDragDrop: true,
    canPasteImage: true,
    showAttachmentButton: true,
    supportsAudio: false,
  },
  voice: {
    isSupported: false,
    isRecording: false,
    isTranscribing: false,
    state: 'idle',
    recordingDuration: 0,
    error: null,
  },
}))

vi.mock('@/hooks/useModelCapabilities', () => ({
  useModelCapabilities: () => ({ inputCapabilities: mocks.caps }),
}))
vi.mock('@/hooks/useVoiceInput', () => ({ useVoiceInput: () => mocks.voice }))
vi.mock('@/services/api/config', () => ({ getModels: vi.fn(() => Promise.resolve({})) }))
vi.mock('@/services/api/files', () => ({
  uploadFile: vi.fn(),
  validateFile: vi.fn(),
}))
vi.mock('@/services/errorReporting', () => ({
  reportError: vi.fn(),
  ErrorType: { SERVER: 'server' },
  ErrorSeverity: { ERROR: 'error' },
}))
vi.mock('@/services/references', () => ({ getReferenceProviders: vi.fn(() => []) }))
vi.mock('@/hooks/useControlledSlotBridge', () => ({
  useControlledSlotBridge: (slot: string) => {
    const bridge = (decl: unknown) => ({ slot, decl })
    bridge.__tag = slot
    return bridge
  },
}))
vi.mock('@/components/schema/DeclaredWidgetLayer', () => ({
  DeclaredWidgetLayer: (props: { slotId?: string; 'data-testid'?: string }) => (
    <div data-testid={props.slotId ? `dw-${props.slotId}` : 'dw-attached'}>
      {props.slotId ?? 'attached'}
    </div>
  ),
}))
vi.mock('@/components/schema/widgets/ContextUsageWidget', () => ({
  ContextUsageWidget: () => <div data-testid="context-usage" />,
}))
vi.mock('../ChatInputActions', () => ({ ChatInputActions: () => null }))

function baseProps() {
  return {
    onSendMessage: vi.fn(),
    modelName: 'MiniMax-M3.1-Flash-Preview',
    enableThinkingMode: true,
  }
}

afterEach(() => {
  window.innerWidth = 1024
})

describe('ChatInput — 作曲器模型信息单入口（P1-1）', () => {
  it('移动视口：两槽位收进单入口（平铺槽位退场），点开面板可达', () => {
    window.innerWidth = 375
    render(<ChatInput {...baseProps()} />)
    // 单入口触发器在场；平铺槽位渲染层退场（不含 model_id/thinking_strength）
    expect(screen.getByTestId('composer-model-entry')).toBeInTheDocument()
    expect(screen.queryByTestId('dw-model_id')).toBeNull()
    expect(screen.queryByTestId('dw-thinking_strength')).toBeNull()

    // 点开：面板在场，两槽位渲染层搬进面板（声明契约不变，仅容器变化）
    fireEvent.click(screen.getByTestId('composer-model-entry'))
    const panel = screen.getByTestId('composer-model-entry-panel')
    expect(panel).toHaveTextContent('模型')
    expect(panel).toHaveTextContent('思考强度')
    expect(screen.getByTestId('dw-model_id')).toBeInTheDocument()
    expect(screen.getByTestId('dw-thinking_strength')).toBeInTheDocument()

    // 再点收起
    fireEvent.click(screen.getByTestId('composer-model-entry'))
    expect(screen.queryByTestId('composer-model-entry-panel')).toBeNull()
  })

  it('桌面视口：两槽位内联平铺契约不变（单入口退场）', () => {
    render(<ChatInput {...baseProps()} />)
    expect(screen.queryByTestId('composer-model-entry')).toBeNull()
    expect(screen.getByTestId('dw-model_id')).toBeInTheDocument()
    expect(screen.getByTestId('dw-thinking_strength')).toBeInTheDocument()
  })

  it('发送键区顶行对齐（P1-1 items-start）：footer 不再垂直居中悬排', () => {
    window.innerWidth = 375
    render(<ChatInput {...baseProps()} />)
    const footer = screen.getByTestId('chat-input-footer')
    expect(footer.className).toContain('items-start')
    expect(footer.className).not.toContain('items-center')
  })
})

/** 模拟壳侧 back（MainActivity 同构：同步 dispatch 后读 detail.consumed）。
 *  dispatch 包 act：关闭器内 setOpen 是 React 态更新，需 act 落 DOM。 */
function dispatchShellBack(): boolean {
  const detail = { consumed: false }
  act(() => {
    window.dispatchEvent(new CustomEvent('lingxi-back-press', { detail }))
  })
  return detail.consumed
}

describe('ChatInput — 单入口面板 back 优先关闭（P1-8 装机复验缺口续修）', () => {
  it('面板开：back 消费且面板关闭（壳不回退不退出）', () => {
    window.innerWidth = 375
    render(<ChatInput {...baseProps()} />)
    fireEvent.click(screen.getByTestId('composer-model-entry'))
    expect(screen.getByTestId('composer-model-entry-panel')).toBeInTheDocument()

    expect(dispatchShellBack()).toBe(true)
    expect(screen.queryByTestId('composer-model-entry-panel')).toBeNull()
  })

  it('面板关：back 不消费（壳走既有历史回退/退出语义，不吞键）', () => {
    window.innerWidth = 375
    render(<ChatInput {...baseProps()} />)
    expect(screen.queryByTestId('composer-model-entry-panel')).toBeNull()
    expect(dispatchShellBack()).toBe(false)
  })

  it('back 关面板后再 back：注册随关闭注销，第二按不消费（面板关→壳语义）', () => {
    window.innerWidth = 375
    render(<ChatInput {...baseProps()} />)
    fireEvent.click(screen.getByTestId('composer-model-entry'))
    expect(dispatchShellBack()).toBe(true)
    expect(screen.queryByTestId('composer-model-entry-panel')).toBeNull()
    expect(dispatchShellBack()).toBe(false)
  })
})
