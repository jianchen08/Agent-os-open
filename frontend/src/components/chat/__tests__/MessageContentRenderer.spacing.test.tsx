/**
 * MessageContentRenderer 分组间距测试
 *
 * 验证 Bug 修复：思考过程、输出文本、工具调用之间的视觉分组间距。
 * - 容器应有 space-y-3 提供统一的 12px 垂直间距
 * - ThinkingDisplay 不应有冗余的 my-2 导致双重间距
 * - 所有 fragment 类型（thinking/text/tool_call）都应有包裹 div 作为 space-y-3 的直接子元素
 */

import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'
import React from 'react'
import type { RenderFragment } from '@/components/chat/hooks/useMessageRender'
import type { MessageToolCall, ThinkingContent } from '@/types/models'
import type { ActivityData } from '@/types/activity'

// Mock MarkdownRenderer
vi.mock('@/components/chat/markdown/MarkdownRenderer', () => ({
  MarkdownRenderer: ({ content }: { content: string }) => (
    <div data-testid="markdown-renderer">{content}</div>
  ),
}))

// Mock LobeChatMarkdown
vi.mock('@/components/chat/LobeChatMarkdown', () => ({
  LobeChatMarkdown: ({ content }: { content: string }) => (
    <div data-testid="lobechat-markdown">{content}</div>
  ),
}))

// Mock ThinkingDisplay — 保留修复后的 className（无 my-2）
vi.mock('@/components/chat/ThinkingDisplay', () => ({
  ThinkingDisplay: ({ thinking }: { thinking: ThinkingContent }) => (
    <div
      className="border-border/50 bg-background/60 overflow-hidden rounded-lg border"
      data-testid="thinking-display"
    >
      {thinking.content}
    </div>
  ),
}))

// Mock ActivityCard
vi.mock('@/components/chat/ActivityCard', () => ({
  default: ({ activity }: { activity: ActivityData }) => (
    <div data-testid="activity-card">{activity.title}</div>
  ),
}))

// Mock enhanceActivityWithToolConfig（由 useMessageRender 间接引用）
vi.mock('@/utils/toolCardRegistry', () => ({
  enhanceActivityWithToolConfig: (activity: ActivityData) => activity,
}))

import { MessageContentRenderer } from '../MessageContentRenderer'

/** 构造合法的 ThinkingContent */
function makeThinking(content: string): ThinkingContent {
  return { content }
}

/** 构造合法的 MessageToolCall */
function makeToolCall(overrides: Partial<MessageToolCall> = {}): MessageToolCall {
  return {
    call_id: 'call-001',
    tool_name: 'file_read',
    tool_args: { path: '/tmp/test.txt' },
    status: 'completed',
    duration_ms: 100,
    ...overrides,
  }
}

/** 构造合法的 ActivityData */
function makeActivity(title: string): ActivityData {
  return {
    type: 'tool_call',
    id: 'act-001',
    title,
    toolName: 'file_read',
    status: 'completed',
  }
}

describe('MessageContentRenderer — 分组间距', () => {
  describe('容器 space-y-3', () => {
    it('渲染容器包含 space-y-3 类名', () => {
      const fragments: RenderFragment[] = [
        { type: 'text', key: 't1', content: 'Hello', sourceId: 'msg1', isLast: false },
      ]
      const { container } = render(
        <MessageContentRenderer fragments={fragments} />,
      )
      const renderer = container.querySelector('.message-content-renderer')
      expect(renderer).toBeInTheDocument()
      expect(renderer?.className).toContain('space-y-3')
    })

    it('空 fragments 列表不渲染容器', () => {
      const { container } = render(
        <MessageContentRenderer fragments={[]} />,
      )
      expect(container.querySelector('.message-content-renderer')).not.toBeInTheDocument()
    })
  })

  describe('多类型 fragment 分组', () => {
    it('thinking + text + tool_call 三种片段都生成包裹 div 作为容器直接子元素', () => {
      const fragments: RenderFragment[] = [
        {
          type: 'thinking',
          key: 'th1',
          thinking: makeThinking('Let me think...'),
          sourceId: 'msg1',
        },
        {
          type: 'text',
          key: 't1',
          content: 'Here is the answer.',
          sourceId: 'msg1',
          isLast: false,
        },
        {
          type: 'tool_call',
          key: 'tc1',
          toolCall: makeToolCall(),
          activity: makeActivity('file_read'),
          index: 0,
          total: 1,
        },
      ]
      const { container } = render(
        <MessageContentRenderer fragments={fragments} />,
      )

      const renderer = container.querySelector('.message-content-renderer')
      expect(renderer).toBeInTheDocument()

      // 每种 fragment 类型都应有包裹 div 作为容器的直接子元素
      // thinking 子 div → 包含 [data-testid="thinking-display"]
      // text 子 div → 包含 [data-testid="lobechat-markdown"]
      // tool_call 子 div → 包含 [data-testid="activity-card"]
      const directChildren = renderer?.children
      expect(directChildren?.length).toBe(3)

      // 验证每个直接子 div 的内容
      const child1 = directChildren?.[0]
      const child2 = directChildren?.[1]
      const child3 = directChildren?.[2]

      expect(child1?.querySelector('[data-testid="thinking-display"]')).toBeTruthy()
      expect(child2?.querySelector('[data-testid="lobechat-markdown"]')).toBeTruthy()
      expect(child3?.querySelector('[data-testid="activity-card"]')).toBeTruthy()
    })

    it('多个 tool_call + text 交替排列也正常分组', () => {
      const fragments: RenderFragment[] = [
        { type: 'text', key: 't1', content: 'Step 1', sourceId: 'msg1', isLast: false },
        {
          type: 'tool_call',
          key: 'tc1',
          toolCall: makeToolCall({ call_id: 'call-1' }),
          activity: makeActivity('file_read'),
          index: 0,
          total: 2,
        },
        { type: 'text', key: 't2', content: 'Step 2', sourceId: 'msg1', isLast: false },
        {
          type: 'tool_call',
          key: 'tc2',
          toolCall: makeToolCall({ call_id: 'call-2', tool_name: 'bash_execute' }),
          activity: makeActivity('bash_execute'),
          index: 1,
          total: 2,
        },
        { type: 'text', key: 't3', content: 'Done', sourceId: 'msg1', isLast: true },
      ]
      const { container } = render(
        <MessageContentRenderer fragments={fragments} />,
      )

      const renderer = container.querySelector('.message-content-renderer')
      expect(renderer).toBeInTheDocument()
      expect(renderer?.children.length).toBe(5)
    })
  })

  describe('ThinkingDisplay 无冗余间距', () => {
    it('ThinkingDisplay 渲染后不包含 my-2（验证修复生效）', () => {
      const fragments: RenderFragment[] = [
        {
          type: 'thinking',
          key: 'th1',
          thinking: makeThinking('思考中...'),
          sourceId: 'msg1',
        },
      ]
      const { container } = render(
        <MessageContentRenderer fragments={fragments} />,
      )
      const thinkingEl = container.querySelector('[data-testid="thinking-display"]')
      expect(thinkingEl).toBeInTheDocument()
      expect(thinkingEl?.className).not.toContain('my-2')
    })
  })
})
