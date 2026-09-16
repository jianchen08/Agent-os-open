/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * PluginMessageCard · 消息卡 message-style 路由测试（模式体系 §5.0 Wave2 件2）
 *
 * 声明→渲染链路：
 * - contributes.chatMessages 声明归一化为 chat/message-style 页（registry 契约）
 * - resolveMessageStyle：样式 id → 声明 + 容器坐标（pluginId/htmlPath）
 * - MessageItem：assistant 消息 metadata.message_style 命中声明 → 通用 webview
 *   消息卡容器；无声明（未声明/禁用）同源消失回退默认渲染；流式期间不路由
 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { PluginMessageCard, resolveMessageStyle } from '../PluginMessageCard'
import { MessageItem } from '../MessageItem'
import type { Message } from '@/types/models'

vi.mock('@/services/api/client', () => ({
  apiClient: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

vi.mock('@/stores/sessionStore', () => ({
  useSessionStore: () => ({ activeSessionId: 'session-1' }),
}))
vi.mock('@/stores/interactionStore', () => ({
  useInteractionStore: (sel: (s: { pendingInteractions: unknown[] }) => unknown) =>
    sel({ pendingInteractions: [] }),
}))
vi.mock('@/hooks/queries/useAgentsQuery', () => ({
  useAgentsQuery: () => ({ data: [] }),
}))
vi.mock('@/services/errorReporting', () => ({
  ErrorType: { CLIENT: 'client' },
  reportError: vi.fn(),
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

import { apiClient } from '@/services/api/client'

/** 注册一条 contributes.chatMessages 形态的归一化声明 */
function registerChatMessageStyle(id: string, pluginId: string, htmlPath?: string): void {
  contributionRegistry.register({
    type: 'chatMessages',
    id,
    title: `样式 ${id}`,
    pluginId,
    props: htmlPath ? { htmlPath } : undefined,
  } as never)
}

function makeAssistantMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'm-1',
    sessionId: 'session-1',
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parts: [],
    ...overrides,
  } as Message
}

describe('resolveMessageStyle — 样式声明解析', () => {
  beforeEach(() => {
    contributionRegistry.clear()
  })

  it('chatMessages 声明命中：返回声明页与容器坐标（props.pluginId 优先于归属插件）', () => {
    registerChatMessageStyle('stage_card', 'plugin_own', '/page/card.html')
    // props.pluginId 未声明 → 回退归属插件
    contributionRegistry.register({
      type: 'chatMessages',
      id: 'inherit_card',
      title: '继承坐标',
      pluginId: 'plugin_inherit',
    } as never)

    const hit = resolveMessageStyle('stage_card')
    expect(hit).not.toBeNull()
    expect(hit?.pluginId).toBe('plugin_own')
    expect(hit?.htmlPath).toBe('/page/card.html')
    expect(hit?.page.id).toBe('stage_card')

    const inherit = resolveMessageStyle('inherit_card')
    expect(inherit?.pluginId).toBe('plugin_inherit')
    expect(inherit?.htmlPath).toBeUndefined()
  })

  it('未声明 / 禁用 / 非法输入 → null（同源消失，调用方回退默认渲染）', () => {
    registerChatMessageStyle('stage_card', 'plugin_own', '/page/card.html')
    expect(resolveMessageStyle('no_such_style')).toBeNull()
    expect(resolveMessageStyle(undefined)).toBeNull()
    expect(resolveMessageStyle('')).toBeNull()
    expect(resolveMessageStyle(42)).toBeNull()
  })
})

describe('PluginMessageCard — 通用 webview 容器渲染', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    vi.mocked(apiClient.get).mockResolvedValue({ data: '<html><body>card</body></html>' })
  })

  it('声明命中 → 渲染 webview 沙箱 iframe（拉取声明 htmlPath 的插件 HTML）', async () => {
    registerChatMessageStyle('rich_card', 'plugin_a', '/page/rich.html')
    render(<PluginMessageCard instanceKey="m-1" styleId="rich_card" />)

    const card = screen.getByTestId('plugin-message-card')
    expect(card).toHaveAttribute('data-message-style', 'rich_card')
    await waitFor(() => {
      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/ext/plugin_a/page/rich.html'),
        expect.anything(),
      )
    })
    await waitFor(() => {
      const iframe = card.querySelector('iframe')
      expect(iframe).not.toBeNull()
      expect(iframe?.getAttribute('srcdoc')).toContain('card')
    })
  })

  it('样式无声明 → 不渲染（零残留）', () => {
    render(<PluginMessageCard instanceKey="m-1" styleId="ghost_style" />)
    expect(screen.queryByTestId('plugin-message-card')).not.toBeInTheDocument()
  })
})

describe('MessageItem — message_style 路由', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    vi.mocked(apiClient.get).mockResolvedValue({ data: '<html><body>card</body></html>' })
  })

  it('assistant 消息携带已声明样式 → 渲染消息卡容器（非默认 markdown 气泡）', () => {
    registerChatMessageStyle('mode_card', 'plugin_mode', '/page/mode-card.html')
    render(
      <MessageItem
        message={makeAssistantMessage({
          metadata: { message_style: 'mode_card' },
        })}
      />,
    )

    expect(screen.getByTestId('plugin-message-card')).toBeInTheDocument()
    expect(screen.queryByTestId('default-markdown')).not.toBeInTheDocument()
  })

  it('样式未声明（禁用同源消失）→ 回退默认渲染', () => {
    render(
      <MessageItem
        message={makeAssistantMessage({
          content: '普通回复',
          metadata: { message_style: 'disabled_style' },
        })}
      />,
    )

    expect(screen.queryByTestId('plugin-message-card')).not.toBeInTheDocument()
    // 默认渲染路径：正文按原样输出（fragments 空时走 displayFallback 文本分支）
    expect(screen.getByText('普通回复')).toBeInTheDocument()
  })

  it('无 message_style 元数据 → 默认渲染（不路由）', () => {
    render(<MessageItem message={makeAssistantMessage({ content: '普通回复' })} />)
    expect(screen.queryByTestId('plugin-message-card')).not.toBeInTheDocument()
  })

  it('流式期间不路由（正文仍在到达），完成后接管', () => {
    registerChatMessageStyle('stream_card', 'plugin_mode', '/page/s.html')
    render(
      <MessageItem
        message={makeAssistantMessage({
          status: 'streaming',
          metadata: { message_style: 'stream_card' },
        })}
        isGenerating
        isLast
      />,
    )
    expect(screen.queryByTestId('plugin-message-card')).not.toBeInTheDocument()
  })
})
