/**
 * 消息内容渲染器
 *
 * 统一的消息内容渲染入口，根据片段类型分发到对应的渲染器
 */

import { memo } from 'react'
import ActivityCard from '@/components/chat/ActivityCard'
import { LobeChatMarkdown } from '@/components/chat/LobeChatMarkdown'
import { MarkdownRenderer } from '@/components/chat/markdown/MarkdownRenderer'
import { ThinkingDisplay } from '@/components/chat/ThinkingDisplay'
import { cn } from '@/lib/utils'
import type { RenderFragment } from '@/components/chat/hooks/useMessageRender'
import type { ReactNode } from 'react'

/**
 * 是否使用 LobeChat Markdown 组件
 * 注意：需要安装依赖 @lobehub/ui 和 motion
 */
const USE_LOBECHAT_MARKDOWN = true

/**
 * 消息内容渲染器 Props
 */
export interface MessageContentRendererProps {
  /** 渲染片段列表 */
  fragments: RenderFragment[]
  /** 是否正在流式输出 */
  isStreaming?: boolean
  /** 自定义类名 */
  className?: string
  /** 自定义文本渲染器 */
  renderText?: (content: string, isStreaming: boolean) => ReactNode
  /** 自定义工具调用渲染器 */
  renderToolCall?: (fragment: Extract<RenderFragment, { type: 'tool_call' }>) => ReactNode
  /** 搜索查询（用于高亮） */
  searchQuery?: string
}

/**
 * 默认文本渲染器
 */
function DefaultTextRenderer(content: string, isStreaming: boolean): ReactNode {
  return <MarkdownRenderer content={content} isStreaming={isStreaming} />
}

/**
 * LobeChat 文本渲染器
 */
function LobeChatTextRenderer(content: string, isStreaming: boolean): ReactNode {
  return <LobeChatMarkdown content={content} isStreaming={isStreaming} />
}

/**
 * 默认工具调用渲染器
 */
function DefaultToolCallRenderer(
  fragment: Extract<RenderFragment, { type: 'tool_call' }>,
): ReactNode {
  return (
    <div key={fragment.key} className="relative">
      {fragment.total > 1 && (
        <div
          className={cn(
            'bg-border/50 absolute left-[14px] w-0.5',
            fragment.index === 0 && 'top-1/2 bottom-0',
            fragment.index > 0 && fragment.index < fragment.total - 1 && 'top-0 bottom-0',
            fragment.index === fragment.total - 1 && fragment.index > 0 && 'top-0 bottom-1/2',
          )}
        />
      )}
      <ActivityCard activity={fragment.activity} />
    </div>
  )
}

/**
 * 渲染单个片段
 */
function renderFragment(
  fragment: RenderFragment,
  isStreaming: boolean,
  renderText?: (content: string, isStreaming: boolean) => ReactNode,
  renderToolCall?: (fragment: Extract<RenderFragment, { type: 'tool_call' }>) => ReactNode,
  searchQuery?: string,
): ReactNode {
  switch (fragment.type) {
    case 'thinking':
      return (
        <div key={fragment.key}>
          <ThinkingDisplay thinking={fragment.thinking} />
        </div>
      )

    case 'text': {
      const isLastStreaming = isStreaming && fragment.isLast
      let content = fragment.content

      if (searchQuery && searchQuery.trim()) {
        content = highlightText(content, searchQuery)
      }

      if (renderText) {
        return <div key={fragment.key}>{renderText(content, isLastStreaming)}</div>
      }
      if (USE_LOBECHAT_MARKDOWN) {
        return (
          <div key={fragment.key}>
            <LobeChatMarkdown content={content} isStreaming={isLastStreaming} />
          </div>
        )
      }
      return <div key={fragment.key}>{DefaultTextRenderer(content, isLastStreaming)}</div>
    }

    case 'tool_call':
      return renderToolCall ? renderToolCall(fragment) : DefaultToolCallRenderer(fragment)

    default:
      return null
  }
}

/**
 * 高亮文本中的搜索关键词
 */
function highlightText(text: string, query: string): string {
  if (!query.trim()) {
    return text
  }

  // 对于 Markdown 内容，我们不直接修改，因为会破坏格式
  // 这里返回原始文本，实际的高亮应该在 Markdown 渲染器中实现
  return text
}

/**
 * 消息内容渲染器基础组件
 */
function MessageContentRendererBase({
  fragments,
  isStreaming = false,
  className,
  renderText,
  renderToolCall,
  searchQuery,
}: MessageContentRendererProps): ReactNode {
  if (fragments.length === 0) {
    return null
  }

  return (
    <div className={cn('message-content-renderer', className)}>
      {fragments.map((fragment) =>
        renderFragment(fragment, isStreaming, renderText, renderToolCall, searchQuery),
      )}
    </div>
  )
}

/**
 * 消息内容渲染器（带 memo 优化）
 *
 * BUG-FIX-fix_20260507_memo_comparison:
 * 问题根因: memo 比较函数只检查 text.content，不检查 toolCall 的 status/result
 *          和 thinking 的 content，导致工具完成后 ActivityCard 不更新。
 * 修复方案: 补全所有 fragment 类型的深度比较。
 */
export const MessageContentRenderer = memo(MessageContentRendererBase, (prev, next) => {
  if (prev.isStreaming !== next.isStreaming) {
    return false
  }

  if (next.isStreaming) {
    return false
  }

  if (prev.fragments.length !== next.fragments.length) {
    return false
  }

  for (let i = 0; i < prev.fragments.length; i++) {
    const prevFragment = prev.fragments[i]
    const nextFragment = next.fragments[i]

    if (prevFragment.type !== nextFragment.type || prevFragment.key !== nextFragment.key) {
      return false
    }

    if (prevFragment.type === 'text' && nextFragment.type === 'text') {
      if (prevFragment.content !== nextFragment.content) {
        return false
      }
    }

    if (prevFragment.type === 'tool_call' && nextFragment.type === 'tool_call') {
      if (prevFragment.toolCall.status !== nextFragment.toolCall.status) return false
      if (prevFragment.toolCall.result !== nextFragment.toolCall.result) return false
      if (prevFragment.toolCall.error !== nextFragment.toolCall.error) return false
      if (prevFragment.toolCall.duration_ms !== nextFragment.toolCall.duration_ms) return false
    }

    if (prevFragment.type === 'thinking' && nextFragment.type === 'thinking') {
      if (prevFragment.thinking.content !== nextFragment.thinking.content) return false
      if (prevFragment.thinking.isThinking !== nextFragment.thinking.isThinking) return false
    }
  }

  return prev.className === next.className && prev.searchQuery === next.searchQuery
})

MessageContentRenderer.displayName = 'MessageContentRenderer'

export default MessageContentRenderer
