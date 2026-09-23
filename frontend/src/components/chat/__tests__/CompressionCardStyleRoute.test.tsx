// @feature: FP-0.2.四 前端Schema(message_style路由) | @ci: frontend-test
// @feature: message-segments P0 | @ci: frontend-test
/**
 * 压缩卡宿主侧验证（消息段模型 P0：压缩原文存档 + 展开，方案 §5.1）。
 *
 * 组件递送走既有全链路零新机制：
 *   插件 manifest contributes.chatMessages 声明（id=样式 id，props.htmlPath 指包内
 *   HTML）→ /api/v1/schema 聚合 → ContributionRegistry.registerFromSchema 归一化为
 *   chat/message-style 槽 → 消息 metadata.message_style = "compression_card" 命中
 *   → MessageItem 路由进通用 webview 消息卡容器（PluginMessageCard）。
 *
 * 压缩块真实形态（P 车道确认）：role=system + name=compressed/state_snapshot +
 * metadata.message_style="compression_card"——路由条件覆盖非流式 system（流式不
 * 路由；无样式 system 仍走默认渲染）。带 compression_ref.segment_id 的 system 块
 * 消息另有宿主侧「查看原始 N 条」入口（CompressionOriginalsButton）。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { resolveMessageStyle } from '../PluginMessageCard'
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
vi.mock('@/services/errorReporting', () => ({
  ErrorType: { CLIENT: 'client' },
  reportError: vi.fn(),
}))
vi.mock('@/components/chat/LobeChatMarkdown', () => ({
  LobeChatMarkdown: ({ content }: { content: string }) => (
    <div data-testid="default-markdown">{content}</div>
  ),
}))

import { apiClient } from '@/services/api/client'

/** context_window_guard plugin.json contributes.chatMessages 的 fixture 形态 */
const COMPRESSION_CARD_DECLARATION = {
  plugin_id: 'context_window_guard',
  plugin_name: '上下文窗口守护',
  contributes: {
    chatMessages: [
      {
        id: 'compression_card',
        title: '上下文压缩卡',
        props: { htmlPath: '/webview/compression_card.html' },
      },
    ],
  },
}

/** 压缩块消息真实形态（P 车道确认）：role=system + name + 内联标签内容 + 段引用 */
function makeCompressionBlockMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'mc-block',
    sessionId: 'session-1',
    sequence: 12,
    role: 'system',
    content:
      '<compressed seq="40-55" level="L1">\n## 过程摘要\n{"轮次": 3}\n</compressed>\n<current_state>\n{"topic": "消息段模型"}\n</current_state>',
    timestamp: new Date().toISOString(),
    parts: [],
    status: 'completed',
    metadata: {
      name: 'compressed',
      message_style: 'compression_card',
      compression_ref: { segment_id: 'seg-block-1', seq_range: [40, 55] },
    },
    ...overrides,
  } as Message
}

function makeAssistantMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'mc-block',
    sessionId: 'session-1',
    role: 'assistant',
    content: '',
    timestamp: new Date().toISOString(),
    parts: [],
    status: 'completed',
    sequence: 12,
    ...overrides,
  } as Message
}

describe('compression_card 压缩卡经 registry 归一化命中 message-style 槽', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    vi.mocked(apiClient.get).mockResolvedValue({ data: '<html><body>compression</body></html>' })
  })

  it('registerFromSchema 真实归一化：chatMessages 声明落位 chat/message-style 槽', () => {
    contributionRegistry.registerFromSchema({
      plugin_contributes: [COMPRESSION_CARD_DECLARATION],
    })

    const page = contributionRegistry
      .getPagesBySpace('chat')
      .find((p) => p.slot === 'message-style' && p.id === 'compression_card')
    expect(page).toBeDefined()
    expect(page?.pluginId).toBe('context_window_guard')
    expect(page?.props).toMatchObject({ htmlPath: '/webview/compression_card.html' })
  })

  it('resolveMessageStyle 命中压缩卡声明（容器渲染坐标齐备）', () => {
    contributionRegistry.registerFromSchema({
      plugin_contributes: [COMPRESSION_CARD_DECLARATION],
    })

    const hit = resolveMessageStyle('compression_card')
    expect(hit).not.toBeNull()
    expect(hit?.pluginId).toBe('context_window_guard')
    expect(hit?.htmlPath).toBe('/webview/compression_card.html')
  })

  it('压缩块真实形态（role=system 块消息）→ 渲染消息卡容器 + 宿主侧查看原始入口', async () => {
    contributionRegistry.registerFromSchema({
      plugin_contributes: [COMPRESSION_CARD_DECLARATION],
    })

    render(<MessageItem message={makeCompressionBlockMessage()} />)

    const card = screen.getByTestId('plugin-message-card')
    expect(card).toHaveAttribute('data-message-style', 'compression_card')
    const item = screen.getByTestId('message-item')
    expect(item).toHaveAttribute('data-role', 'system')
    // 宿主侧「查看原始 N 条」：N = compression_ref.seq_range 跨度（40-55 → 16）
    expect(screen.getByTestId('compression-originals-button')).toHaveTextContent('查看原始 16 条')
    // 卡片 HTML 按声明从插件包内拉取（/ext/<pluginId><htmlPath>）
    await waitFor(() => {
      expect(apiClient.get).toHaveBeenCalledWith(
        expect.stringContaining('/ext/context_window_guard/webview/compression_card.html'),
        expect.anything(),
      )
    })
  })

  it('assistant 消息携带已声明样式仍路由（原形态回归钉）', () => {
    contributionRegistry.registerFromSchema({
      plugin_contributes: [COMPRESSION_CARD_DECLARATION],
    })

    render(
      <MessageItem
        message={makeAssistantMessage({ metadata: { message_style: 'compression_card' } })}
      />,
    )
    const card = screen.getByTestId('plugin-message-card')
    expect(card).toHaveAttribute('data-message-style', 'compression_card')
    expect(screen.getByTestId('message-item')).toHaveAttribute('data-role', 'assistant')
    // assistant 无宿主侧查看原始入口（仅 system 块消息）
    expect(screen.queryByTestId('compression-originals-button')).not.toBeInTheDocument()
  })

  it('流式 system 块消息不路由（正文仍在到达，完成后接管）', () => {
    contributionRegistry.registerFromSchema({
      plugin_contributes: [COMPRESSION_CARD_DECLARATION],
    })

    render(<MessageItem message={makeCompressionBlockMessage({ status: 'streaming' })} />)
    expect(screen.queryByTestId('plugin-message-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('compression-originals-button')).not.toBeInTheDocument()
  })

  it('声明缺席（插件禁用同源消失）→ 压缩块消息回退默认渲染（零残留）', () => {
    render(<MessageItem message={makeCompressionBlockMessage()} />)
    expect(screen.queryByTestId('plugin-message-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('compression-originals-button')).not.toBeInTheDocument()
  })
})
