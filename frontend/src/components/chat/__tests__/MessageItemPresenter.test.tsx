// @feature FP-T12 角色扮演成熟化 Wave B（扮演气泡呈现） @ci frontend-test
/**
 * MessageItem 扮演气泡呈现测试：
 * - presenter 命中（emoji 头像）：徽标显示卡名+emoji、头像位渲染 emoji；
 * - presenter 命中（{fg,bg} 色对）：头像位渲染名字首字+fg 字色 bg 底，徽标仅卡名；
 * - presenter 未命中：回退注册表徽标/默认头像（非扮演消息布局零变化）；
 * - 各消息态（流式）同样按 presenter 切换；用户消息不受 presenter 影响；
 * - 附身激活（无 agentId）：assistant 气泡按 possessed 档切换（emoji/色对），
 *   有 agentId 卡键则 presenter 优先，作用域限活跃管道会话，解除回退基线；
 * - 扮演呈现态（presenter 命中或附身生效）工具消息：默认折叠为一行叙事行
 *   （失败态「行动受挫」），点击展开/收回原生 ActivityCard；非扮演态工具卡
 *   原样直出（零变化）；
 * - AI 消息编辑保存：诚实 toast「消息编辑通道未接线」，不走 user 编辑重发链路。
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useRoleplayPossessStore } from '@/stores/roleplayPossessStore'
import { renderWithProviders } from '@/test/renderWithProviders'
import { MessageItem } from '../MessageItem'
import type { PresenterProfile } from '@/services/api/presenterProfiles'
import type { RoleplayPossession } from '@/services/schema/modeOptions'
import type { Message } from '@/types/models'

/**
 * presenter 档案桩（可变状态，工厂闭包读取；null=未命中回退老路）。
 * 附身/管道两 store 用真实实例：zustand 订阅驱动 memo 组件随 setState 重渲染，
 * 与生产行为一致；测试内 setState 写态、afterEach 复位。
 */
const presenterState = vi.hoisted(() => ({
  profile: null as PresenterProfile | null,
}))
vi.mock('@/services/api/presenterProfiles', () => ({
  usePresenterProfile: () => presenterState.profile,
}))

/** agents 注册表桩（可变清单） */
const agentsState = vi.hoisted(() => ({
  list: [] as { id: string; name: string }[],
}))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: () => ({ data: agentsState.list }),
}))

vi.mock('sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn() },
}))

vi.mock('@/stores/sessionStore', async () => (await import('./helpers/messageItemMocks')).sessionStoreActiveMock())
vi.mock('@/stores/interactionStore', () => ({
  useInteractionStore: (sel: (s: { pendingInteractions: unknown[] }) => unknown) =>
    sel({ pendingInteractions: [] }),
}))
vi.mock('@/services/errorReporting', async () => (await import('./helpers/messageItemMocks')).errorReportingMock())
vi.mock('@/components/chat/MessageContentRenderer', () => ({ default: () => null }))
// 断开 markdown 渲染器的 emoji-mart 重依赖链（与既有 MessageItem 测试同口径）
vi.mock('@/components/chat/LobeChatMarkdown', async () => (await import('./helpers/messageItemMocks')).lobeMarkdownStubMock())

/** MessageActions 测试替身：编辑入口暴露为按钮（真实组件行为归其自身测试） */
vi.mock('@/components/chat/MessageActions', () => ({
  MessageActions: (props: Record<string, ((...a: unknown[]) => void) | undefined>) => (
    <div data-testid="actions">
      <button onClick={() => props.onEdit?.()}>A-edit</button>
    </div>
  ),
}))

/** useMessageRender 桩：直出原文（fragments 空 → 走 displayFallback 文本路） */
vi.mock('@/components/chat/hooks/useMessageRender', () => ({
  default: (args: { versionContent: string | null; message: { content: string } }) => ({
    fragments: [],
    displayContent: args.versionContent ?? args.message.content,
  }),
}))

import { toast } from 'sonner'

function makeMessage(partial: Partial<Message>): Message {
  return {
    id: 'm-1',
    sessionId: 'session-1',
    sequence: 1,
    role: 'assistant',
    content: '晚上好',
    timestamp: new Date().toISOString(),
    status: 'completed',
    ...partial,
  } as Message
}

const ROLEPLAY_AGENT_ID = 'mode_roleplay/card_luna'

/** 附身态基线：活跃管道 pipeline-1 归属 session-1（与消息桩同 session） */
function setPipelineScope(sessionId: string) {
  usePipelineMessageStore.setState({
    activePipelineId: 'pipeline-1',
    pipelineSessionMap: { 'pipeline-1': sessionId },
  })
}

beforeEach(() => {
  setPipelineScope('session-1')
})

afterEach(() => {
  vi.clearAllMocks()
  presenterState.profile = null
  agentsState.list = []
  useRoleplayPossessStore.setState({ possessed: null })
  setPipelineScope('session-1')
})

/** 附身档工厂（store 契约必要项：card_id/name 非空 + avatar 两形态） */
function makePossession(partial: Partial<RoleplayPossession> = {}): RoleplayPossession {
  return {
    card_id: 'card_luna',
    name: '月见',
    avatar: '🌙',
    personaText: '',
    ...partial,
  }
}

describe('MessageItem 扮演徽标与头像位', () => {
  it('presenter 命中（emoji 头像）：徽标显示卡名+emoji，头像位渲染 emoji（注册表未命中也出徽标）', () => {
    presenterState.profile = { name: '月见', avatar: '🌙', origin: 'mode_roleplay' }
    const { container } = renderWithProviders(
      <MessageItem message={makeMessage({ agentId: ROLEPLAY_AGENT_ID })} />,
    )
    // 徽标：卡名 + emoji 前位，样式复用 agent 徽标同一套 className
    const badge = screen.getByText('月见').parentElement as HTMLElement
    expect(badge.textContent).toBe('🌙月见')
    expect(badge.className).toContain('badge-info-bg')
    // 头像位：28px 徽章渲染 emoji（替代默认 Bot 头像）
    const avatarEl = screen.getByTestId('presenter-avatar')
    expect(avatarEl.textContent).toBe('🌙')
    expect(avatarEl.className).toContain('h-7 w-7')
    expect(avatarEl.getAttribute('style')).toContain('--secondary')
    // 默认 Bot 头像位被替换（同一槽位不再渲染 AvatarFallback）
    expect(container.querySelectorAll('[data-radix-collection-item], .rounded-xl.shadow-sm')).toHaveLength(0)
  })

  it('presenter 命中（色对头像）：头像位渲染名字首字+fg 字色 bg 底，徽标仅显示卡名', () => {
    presenterState.profile = {
      name: '月见',
      avatar: { fg: '#ff0000', bg: '#0000ff' },
      origin: 'mode_roleplay',
    }
    renderWithProviders(<MessageItem message={makeMessage({ agentId: ROLEPLAY_AGENT_ID })} />)
    const avatarEl = screen.getByTestId('presenter-avatar')
    expect(avatarEl.textContent).toBe('月')
    expect(avatarEl.style.color).toBe('rgb(255, 0, 0)')
    expect(avatarEl.getAttribute('style')).toMatch(/#0000ff|rgb\(0, 0, 255\)/)
    // 色对形态不动徽标底色：徽标只含卡名，无 emoji 前位
    expect(screen.getByText('月见').parentElement?.textContent).toBe('月见')
  })

  it('presenter 未命中：回退注册表徽标（Sparkles+agent 名）与默认头像，无头像位', () => {
    agentsState.list = [{ id: 'agentos', name: '总控' }]
    const { container } = renderWithProviders(
      <MessageItem message={makeMessage({ agentId: 'agentos' })} />,
    )
    const badge = screen.getByText('总控').parentElement as HTMLElement
    expect(badge.className).toContain('badge-info-bg')
    expect(badge.querySelector('svg')).not.toBeNull()
    expect(screen.queryByTestId('presenter-avatar')).toBeNull()
    // 默认头像回落（rounded-xl 阴影 Avatar 仍在布局中）
    expect(container.querySelector('.rounded-xl.shadow-sm')).not.toBeNull()
  })

  it('presenter 与注册表双未命中：无徽标无头像位（非扮演消息布局零变化）', () => {
    renderWithProviders(<MessageItem message={makeMessage({ content: '普通回答' })} />)
    expect(screen.queryByText('月见')).toBeNull()
    expect(screen.queryByTestId('presenter-avatar')).toBeNull()
    expect(screen.getByText('普通回答')).toBeInTheDocument()
  })

  it('流式态 assistant 消息同样按 presenter 切换徽标与头像位', () => {
    presenterState.profile = { name: '月见', avatar: '🌙', origin: 'mode_roleplay' }
    renderWithProviders(
      <MessageItem message={makeMessage({ agentId: ROLEPLAY_AGENT_ID, content: '', status: 'streaming' })} />,
    )
    expect(screen.getByText('月见')).toBeInTheDocument()
    expect(screen.getByTestId('presenter-avatar').textContent).toBe('🌙')
    expect(screen.getByText('思考中...')).toBeInTheDocument()
  })

  it('用户消息不出现扮演徽标与头像位（用户侧后置，气泡不动）', () => {
    presenterState.profile = { name: '月见', avatar: '🌙', origin: 'mode_roleplay' }
    renderWithProviders(
      <MessageItem
        message={makeMessage({ role: 'user', agentId: ROLEPLAY_AGENT_ID, content: '你好' })}
      />,
    )
    expect(screen.queryByTestId('presenter-avatar')).toBeNull()
    expect(screen.queryByText('月见')).toBeNull()
    expect(screen.getByText('你好')).toBeInTheDocument()
  })
})

describe('MessageItem 附身态头像切换', () => {
  it('附身激活 + 无 agentId（emoji）：头像位与徽标切为附身档，默认头像被替换', () => {
    useRoleplayPossessStore.setState({ possessed: makePossession({ avatar: '🌙' }) })
    const { container } = renderWithProviders(
      <MessageItem message={makeMessage({ content: '附身回答' })} />,
    )
    // 头像位：28px 徽章渲染附身 emoji（替代默认 Bot 头像）
    const avatarEl = screen.getByTestId('presenter-avatar')
    expect(avatarEl.textContent).toBe('🌙')
    expect(avatarEl.className).toContain('h-7 w-7')
    // 徽标：emoji 前位 + 附身名
    expect(screen.getByText('月见').parentElement?.textContent).toBe('🌙月见')
    // 默认 Bot 头像位被替换（同一槽位不再渲染 Avatar）
    expect(container.querySelector('.rounded-xl.shadow-sm')).toBeNull()
  })

  it('附身激活 + 无 agentId（色对）：头像位渲染名字首字+fg 字色 bg 底，徽标仅附身名', () => {
    useRoleplayPossessStore.setState({
      possessed: makePossession({ avatar: { fg: '#ff0000', bg: '#0000ff' } }),
    })
    renderWithProviders(<MessageItem message={makeMessage({ content: '附身回答' })} />)
    const avatarEl = screen.getByTestId('presenter-avatar')
    expect(avatarEl.textContent).toBe('月')
    expect(avatarEl.style.color).toBe('rgb(255, 0, 0)')
    expect(avatarEl.getAttribute('style')).toMatch(/#0000ff|rgb\(0, 0, 255\)/)
    expect(screen.getByText('月见').parentElement?.textContent).toBe('月见')
  })

  it('附身激活 + 有 agentId 卡键：presenter 解析优先，不被附身档覆盖', () => {
    presenterState.profile = { name: '卡上名', avatar: '🎭', origin: 'mode_roleplay' }
    useRoleplayPossessStore.setState({ possessed: makePossession({ name: '附身名', avatar: '👻' }) })
    renderWithProviders(<MessageItem message={makeMessage({ agentId: ROLEPLAY_AGENT_ID })} />)
    expect(screen.getByText('卡上名')).toBeInTheDocument()
    expect(screen.queryByText('附身名')).toBeNull()
    expect(screen.getByTestId('presenter-avatar').textContent).toBe('🎭')
  })

  it('附身作用域外（消息 session ≠ 活跃管道 session）不随附身切换', () => {
    useRoleplayPossessStore.setState({ possessed: makePossession() })
    // 活跃管道归属另一会话：session 失配 → 消息行不进入附身覆盖
    setPipelineScope('session-2')
    renderWithProviders(<MessageItem message={makeMessage({ content: '他山消息' })} />)
    expect(screen.queryByText('月见')).toBeNull()
    expect(screen.queryByTestId('presenter-avatar')).toBeNull()
    expect(screen.getByText('他山消息')).toBeInTheDocument()
  })

  it('附身解除 → 回退基线渲染（与未附身前 DOM 一致，零变化）', () => {
    const message = makeMessage({ content: '普通回答' })
    const { container, rerender } = renderWithProviders(<MessageItem message={message} />)
    const baseline = container.innerHTML
    act(() => {
      useRoleplayPossessStore.setState({ possessed: makePossession() })
    })
    rerender(<MessageItem message={message} />)
    // 附身生效：头像位切为附身 emoji
    expect(container.innerHTML).not.toBe(baseline)
    expect(screen.getByTestId('presenter-avatar').textContent).toBe('🌙')
    // 解除：possessed=null → 与基线一致（possessed=null 稳定引用，渲染零变化）
    act(() => {
      useRoleplayPossessStore.setState({ possessed: null })
    })
    rerender(<MessageItem message={message} />)
    expect(container.innerHTML).toBe(baseline)
  })
})

describe('MessageItem AI 消息编辑（一期诚实未接线）', () => {
  it('保存不走 user 编辑重发链路（onEdit 不被调）并诚实 toast', async () => {
    const onEdit = vi.fn().mockResolvedValue(undefined)
    renderWithProviders(
      <MessageItem message={makeMessage({ agentId: ROLEPLAY_AGENT_ID })} onEdit={onEdit} />,
    )
    fireEvent.click(screen.getByText('A-edit'))
    const textarea = await screen.findByRole('textbox')
    fireEvent.change(textarea, { target: { value: '改写的回复' } })
    fireEvent.keyDown(textarea, { key: 'Enter', ctrlKey: true })
    // 编辑器关闭、通道未接线的诚实提示、绝不触发 user 截断重发回调
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(toast.error).toHaveBeenCalledWith('消息编辑通道未接线')
    expect(onEdit).not.toHaveBeenCalled()
  })
})

describe('MessageItem 扮演态工具消息卡片沉浸化', () => {
  /** 工具消息桩（isTool 分支最小契约字段，status 缺省按 completed 渲染） */
  function makeToolMessage(partial: Partial<Message> = {}): Message {
    return makeMessage({
      role: 'tool',
      content: '',
      toolName: 'read_file',
      toolCallId: 'call-1',
      ...partial,
    })
  }

  it('扮演态（卡键 presenter 命中）工具消息：默认折叠为低调叙事行，ActivityCard 不直出', () => {
    presenterState.profile = { name: '月见', avatar: '🌙', origin: 'mode_roleplay' }
    const { container } = renderWithProviders(
      <MessageItem message={makeToolMessage({ agentId: ROLEPLAY_AGENT_ID })} />,
    )
    const row = screen.getByTestId('roleplay-tool-narrative')
    expect(row.textContent).toBe('✦ 月见的静默行动')
    expect(row.className).toContain('text-muted-foreground')
    expect(row.className).toContain('text-xs')
    // 满宽工具卡被叙事行取代（默认折叠）
    expect(container.querySelector('[data-activity-type]')).toBeNull()
  })

  it('附身态工具消息（无卡键）：折叠行显示附身档角色名，ActivityCard 不直出', () => {
    useRoleplayPossessStore.setState({ possessed: makePossession() })
    const { container } = renderWithProviders(<MessageItem message={makeToolMessage()} />)
    expect(screen.getByTestId('roleplay-tool-narrative').textContent).toBe('✦ 月见的静默行动')
    expect(container.querySelector('[data-activity-type]')).toBeNull()
  })

  it('点击折叠行 → 原生 ActivityCard 展开可查证，再点收回', () => {
    presenterState.profile = { name: '月见', avatar: '🌙', origin: 'mode_roleplay' }
    const { container } = renderWithProviders(
      <MessageItem message={makeToolMessage({ agentId: ROLEPLAY_AGENT_ID })} />,
    )
    fireEvent.click(screen.getByTestId('roleplay-tool-narrative'))
    const card = container.querySelector('[data-activity-type="tool_call"]')
    expect(card).not.toBeNull()
    expect(card?.getAttribute('data-activity-status')).toBe('completed')
    fireEvent.click(screen.getByTestId('roleplay-tool-narrative'))
    expect(container.querySelector('[data-activity-type]')).toBeNull()
  })

  it('非扮演态工具消息：ActivityCard 原样直出，无折叠行（默认展开布局零变化）', () => {
    const { container } = renderWithProviders(<MessageItem message={makeToolMessage()} />)
    expect(screen.queryByTestId('roleplay-tool-narrative')).toBeNull()
    const card = container.querySelector('[data-activity-type="tool_call"]')
    expect(card).not.toBeNull()
    expect(card?.getAttribute('data-activity-status')).toBe('completed')
  })

  it('失败态工具消息折叠行：「✦ 行动受挫」弱警示色，展开仍走原生卡', () => {
    presenterState.profile = { name: '月见', avatar: '🌙', origin: 'mode_roleplay' }
    const { container } = renderWithProviders(
      <MessageItem
        message={makeToolMessage({
          agentId: ROLEPLAY_AGENT_ID,
          status: 'failed',
          toolError: '文件不存在',
        })}
      />,
    )
    const row = screen.getByTestId('roleplay-tool-narrative')
    expect(row.textContent).toBe('✦ 行动受挫')
    expect(row.className).toContain('text-status-warning/70')
    fireEvent.click(row)
    expect(container.querySelector('[data-activity-status="failed"]')).not.toBeNull()
  })
})
