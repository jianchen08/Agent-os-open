/** @feature FP-RP面板v3.1刀3 | @ci: frontend-test */
/**
 * MessageItem · 本体形态分发门测试（ADR 2026-10-14-message-card-body-form）
 *
 * 契约：会话 mode 绑定 → message_cards bubble 声明命中 → 该会话全部已定稿
 * user/assistant 消息行本体交插件卡（fit=auto）；流式/降级态/等待占位/附件/
 * 引用片/非纯文本片段行保持宿主原生。兼容态锚（对照锁定）：非 RP 会话或无
 * bubble 声明 → 渲染与现状一致（默认渲染、零卡零标记）。
 */
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { apiClient } from '@/services/api/client'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { MessageItem } from '../MessageItem'
import type { Message } from '@/types/models'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn() },
}))
vi.mock('@/stores/sessionStore', () => {
  const state = { activeSessionId: 'session-1' }
  const useSessionStore = Object.assign(() => state, { getState: () => state })
  return { useSessionStore }
})
vi.mock('@/stores/interactionStore', () => ({
  useInteractionStore: (sel: (s: { pendingInteractions: unknown[] }) => unknown) =>
    sel({ pendingInteractions: [] }),
}))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: () => ({ data: [] }),
}))
vi.mock('@/services/errorReporting', async () => (await import('./helpers/messageItemMocks')).errorReportingMock())
vi.mock('@/services/api/presenterProfiles', () => ({
  usePresenterProfile: vi.fn(() => null),
}))
vi.mock('@/components/chat/LobeChatMarkdown', () => ({
  LobeChatMarkdown: ({ content }: { content: string }) => (
    <div data-testid="default-markdown">{content}</div>
  ),
}))
vi.mock('@/components/chat/MessageContentRenderer', () => ({
  default: ({ fragments }: { fragments: unknown[] }) => (
    <div data-testid="fragments-render">{fragments.length} fragments</div>
  ),
}))

/** 按 /api/v1/schema 形态注册 message_cards + message-style 页声明（缺省带
 *  modes 门——现役插件声明形态；modes 显式传 [] 或省略键测试域不限语义） */
function registerBubbleDeclaration(options: { withPage?: boolean; modes?: string[] | null } = {}): void {
  const pages =
    options.withPage === false
      ? []
      : [
          {
            id: 'roleplay_bubble_card',
            title: '对话气泡',
            space: 'chat',
            slot: 'message-style',
            widget: 'webview',
            props: { htmlPath: 'webview/rp_bubble.html' },
          },
        ]
  const match: Record<string, unknown> = { kind: 'bubble' }
  if (options.modes !== null) match.modes = options.modes ?? ['roleplay']
  contributionRegistry.registerFromSchema({
    plugin_contributes: [
      {
        plugin_id: 'mode_roleplay',
        contributes: {
          pages,
          message_cards: [{ match, style_id: 'roleplay_bubble_card' }],
        },
      },
    ],
  })
}

/** 会话执行选项快照（modeBinding 会话域门数据源 = localStorage 快照） */
function bindSessionMode(sessionId: string, mode: string | null): void {
  if (mode === null) {
    localStorage.removeItem(`session-exec-options:${sessionId}`)
    return
  }
  localStorage.setItem(
    `session-exec-options:${sessionId}`,
    JSON.stringify({ values: {}, modeBinding: { mode } }),
  )
}

function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'm-1',
    sessionId: 'session-1',
    role: 'assistant',
    content: '夜色沉入湖面。',
    timestamp: new Date().toISOString(),
    parts: [],
    status: 'completed',
    ...overrides,
  } as Message
}

beforeEach(() => {
  contributionRegistry.clear()
  localStorage.clear()
  vi.mocked(apiClient.get).mockResolvedValue({ data: '<html><body>card</body></html>' })
})

describe('MessageItem — 本体形态分发（bubble 命中域内）', () => {
  it('RP 会话已定稿 assistant 行 → 本体交插件卡（fit=auto + 行标记 + 元信息行保留）', () => {
    registerBubbleDeclaration()
    bindSessionMode('session-1', 'roleplay')
    const { container } = render(<MessageItem message={makeMessage()} />)

    const row = container.querySelector('[data-message-card-body="roleplay_bubble_card"]')
    expect(row).not.toBeNull()
    expect(screen.getByTestId('plugin-message-card')).toHaveAttribute('data-fit', 'auto')
    // 兼容态锚的另一面：本体卡接管后默认渲染消失
    expect(screen.queryByTestId('default-markdown')).not.toBeInTheDocument()
    // 元信息行保留宿主（观感衔接锚：时间戳/操作区不进卡）
    expect(row?.querySelector('.text-muted-foreground\\/70')).not.toBeNull()
  })

  it('RP 会话 user 行 → 同样本体卡化（user/assistant 双形态）', () => {
    registerBubbleDeclaration()
    bindSessionMode('session-1', 'roleplay')
    const { container } = render(
      <MessageItem message={makeMessage({ role: 'user', content: '我推门而入。' })} />,
    )
    expect(container.querySelector('[data-message-card-body]')).not.toBeNull()
  })

  it('流式中的行不接卡（宿主原生渲染零改动，刀3 降格裁定）', () => {
    registerBubbleDeclaration()
    bindSessionMode('session-1', 'roleplay')
    const { container } = render(
      <MessageItem message={makeMessage({ status: 'streaming' })} isGenerating isLast />,
    )
    expect(container.querySelector('[data-message-card-body]')).toBeNull()
    expect(screen.queryByTestId('plugin-message-card')).not.toBeInTheDocument()
  })

  it('降级态（error）行不接卡（错误透传是宿主语义）', () => {
    registerBubbleDeclaration()
    bindSessionMode('session-1', 'roleplay')
    const { container } = render(<MessageItem message={makeMessage({ status: 'error' })} />)
    expect(container.querySelector('[data-message-card-body]')).toBeNull()
  })

  it('user 行带附件不接卡（附件画廊是宿主功能面）', () => {
    registerBubbleDeclaration()
    bindSessionMode('session-1', 'roleplay')
    const { container } = render(
      <MessageItem
        message={makeMessage({
          role: 'user',
          attachments: [{ id: 'a1', name: '图.png', url: '/uploads/a.png', type: 'image/png' }],
        })}
      />,
    )
    expect(container.querySelector('[data-message-card-body]')).toBeNull()
  })

  it('非纯文本片段（thinking part）行不接卡（片段面是宿主功能面）', () => {
    registerBubbleDeclaration()
    bindSessionMode('session-1', 'roleplay')
    const { container } = render(
      <MessageItem
        message={makeMessage({
          parts: [{ type: 'thinking', content: '推演剧情', state: 'completed' }],
        })}
      />,
    )
    expect(container.querySelector('[data-message-card-body]')).toBeNull()
  })
})

describe('MessageItem — 兼容态对照（无声明/域外 = 现状零变化）', () => {
  it('声明在册（带 modes 门）但会话无 mode 绑定（非 RP 会话）→ 默认渲染零卡零标记', () => {
    registerBubbleDeclaration()
    bindSessionMode('session-1', null)
    const { container } = render(<MessageItem message={makeMessage()} />)

    expect(container.querySelector('[data-message-card-body]')).toBeNull()
    expect(screen.queryByTestId('plugin-message-card')).not.toBeInTheDocument()
    // 默认渲染路径：fragments 空时正文走 displayFallback 文本分支（与现状一致）
    expect(screen.getByText('夜色沉入湖面。')).toBeInTheDocument()
  })

  it('会话域门外（modes 不含活跃 mode）→ 默认渲染', () => {
    registerBubbleDeclaration({ modes: ['writing'] })
    bindSessionMode('session-1', 'roleplay')
    const { container } = render(<MessageItem message={makeMessage()} />)

    expect(container.querySelector('[data-message-card-body]')).toBeNull()
    expect(screen.getByText('夜色沉入湖面。')).toBeInTheDocument()
  })

  it('modes 缺省 = 会话域不限：无 mode 绑定的会话也命中（契约正例）', () => {
    registerBubbleDeclaration({ modes: null })
    bindSessionMode('session-1', null)
    const { container } = render(<MessageItem message={makeMessage()} />)

    expect(container.querySelector('[data-message-card-body="roleplay_bubble_card"]')).not.toBeNull()
  })

  it('message_cards 声明缺失（无 bubble 条目）→ 默认渲染', () => {
    bindSessionMode('session-1', 'roleplay')
    const { container } = render(<MessageItem message={makeMessage()} />)

    expect(container.querySelector('[data-message-card-body]')).toBeNull()
    expect(screen.getByText('夜色沉入湖面。')).toBeInTheDocument()
  })

  it('bubble 声明在场但 message-style 页声明缺失（插件半禁用同源消失）→ 默认渲染', () => {
    registerBubbleDeclaration({ withPage: false })
    bindSessionMode('session-1', 'roleplay')
    const { container } = render(<MessageItem message={makeMessage()} />)

    expect(container.querySelector('[data-message-card-body]')).toBeNull()
    expect(screen.getByText('夜色沉入湖面。')).toBeInTheDocument()
  })

  it('追加形态声明（marker）与本刀互不干扰：marker 声明在册无标记载荷 → 默认渲染', () => {
    contributionRegistry.registerFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'mode_roleplay',
          contributes: {
            message_cards: [{ match: { marker: 'state' }, style_id: 'roleplay_state_card' }],
          },
        },
      ],
    })
    bindSessionMode('session-1', 'roleplay')
    const { container } = render(<MessageItem message={makeMessage()} />)

    expect(container.querySelector('[data-message-card-body]')).toBeNull()
    expect(screen.getByText('夜色沉入湖面。')).toBeInTheDocument()
  })
})
