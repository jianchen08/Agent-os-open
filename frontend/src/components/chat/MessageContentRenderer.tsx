/**
 * 消息内容渲染器
 *
 * 统一的消息内容渲染入口，根据片段类型分发到对应的渲染器
 */

import ActivityCard from '@/components/chat/ActivityCard'
import { LobeChatMarkdown } from '@/components/chat/LobeChatMarkdown'
import { ThinkingDisplay } from '@/components/chat/ThinkingDisplay'
import type { RenderFragment } from '@/components/chat/hooks/useMessageRender'
import { MarkdownRenderer } from '@/components/chat/markdown/MarkdownRenderer'
import { cn } from '@/lib/utils'
import type { ReactNode } from 'react'
import { memo } from 'react'

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
}

/**
 * 默认文本渲染器
 */
function DefaultTextRenderer(content: string, isStreaming: boolean): ReactNode {
  return (
    <MarkdownRenderer
      content={content}
      isStreaming={isStreaming}
    />
  )
}

/**
 * LobeChat 文本渲染器
 */
function LobeChatTextRenderer(content: string, isStreaming: boolean): ReactNode {
  return (
    <LobeChatMarkdown content={content} isStreaming={isStreaming} />
  )
}

/**
 * 默认工具调用渲染器
 */
function DefaultToolCallRenderer(
  fragment: Extract<RenderFragment, { type: 'tool_call' }>
): ReactNode {
  return (
    <div key={fragment.key} className="relative">
      {fragment.total > 1 && (
        <div
          className={cn(
            'absolute left-[14px] w-0.5 bg-border/50',
            fragment.index === 0 && 'top-1/2 bottom-0',
            fragment.index > 0 && fragment.index < fragment.total - 1 && 'top-0 bottom-0',
            fragment.index === fragment.total - 1 && fragment.index > 0 && 'top-0 bottom-1/2'
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
  renderToolCall?: (fragment: Extract<RenderFragment, { type: 'tool_call' }>) => ReactNode
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
      if (renderText) {
        return <div key={fragment.key}>{renderText(fragment.content, isLastStreaming)}</div>
      }
      if (USE_LOBECHAT_MARKDOWN) {
        return <div key={fragment.key}>{LobeChatTextRenderer(fragment.content, isLastStreaming)}</div>
      }
      return <div key={fragment.key}>{DefaultTextRenderer(fragment.content, isLastStreaming)}</div>
    }

    case 'tool_call':
      return renderToolCall
        ? renderToolCall(fragment)
        : DefaultToolCallRenderer(fragment)

    default:
      return null
  }
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
}: MessageContentRendererProps): ReactNode {
  if (fragments.length === 0) {
    return null
  }

  return (
    <div className={cn('message-content-renderer', className)}>
      {fragments.map(fragment =>
        renderFragment(fragment, isStreaming, renderText, renderToolCall)
      )}
    </div>
  )
}

/**
 * 消息内容渲染器（带 memo 优化）
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
  }

  return prev.className === next.className
})

MessageContentRenderer.displayName = 'MessageContentRenderer'

export default MessageContentRenderer
