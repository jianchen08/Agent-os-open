/**
 * 消息列表组件
 *
 * 显示消息列表，支持虚拟滚动、自动滚动、分页加载和加载状态
 * 使用 Virtuoso 实现高性能虚拟滚动
 */

import { Loader2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef } from 'react'
import { Virtuoso, type VirtuosoHandle } from 'react-virtuoso'
import { MessageItem } from './MessageItem'
import type { MessageListProps } from './types'

/**
 * 消息列表组件属性扩展
 */
export interface ExtendedMessageListProps extends MessageListProps {
  /** 是否还有更多消息 */
  hasMore?: boolean
  /** 是否正在加载更多 */
  isLoadingMore?: boolean
  /** 加载更多回调 */
  onLoadMore?: () => void
  /** 会话ID */
  sessionId?: string
}

/**
 * 消息列表组件
 */
export const MessageList = ({
  messages,
  isGenerating = false,
  onRegenerate,
  onEdit,
  onDelete,
  modelName,
  className = '',
  hasMore = false,
  isLoadingMore = false,
  onLoadMore,
  searchQuery,
}: ExtendedMessageListProps) => {
  const virtuosoRef = useRef<VirtuosoHandle>(null)
  const isUserScrolling = useRef(false)
  const lastMessageCount = useRef(messages.length)
  const scrollTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const resizeObserverRef = useRef<ResizeObserver | null>(null)
  const initialScrollDone = useRef(false)
  const isAtTopRef = useRef(false)

  /**
   * 滚动到底部
   */
  const scrollToBottom = useCallback((behavior: 'smooth' | 'auto' = 'smooth') => {
    if (virtuosoRef.current) {
      virtuosoRef.current.scrollToIndex({
        index: 'LAST',
        behavior,
        align: 'end',
      })
    }
  }, [])

  /**
   * 处理用户滚动
   */
  const handleScroll = useCallback(
    (scrollTop: number, isScrolling: boolean) => {
      if (isScrolling) {
        isUserScrolling.current = true
        if (scrollTimeoutRef.current) {
          clearTimeout(scrollTimeoutRef.current)
        }
        scrollTimeoutRef.current = setTimeout(() => {
          isUserScrolling.current = false
        }, 500)
      }

      const atTop = scrollTop < 50
      isAtTopRef.current = atTop

      if (atTop && hasMore && !isLoadingMore && onLoadMore && !isScrolling) {
        onLoadMore()
      }
    },
    [hasMore, isLoadingMore, onLoadMore],
  )

  /**
   * 处理消息变化：新消息到达时自动滚动到底部
   */
  useEffect(() => {
    const messageCount = messages.length
    const hasNewMessages = messageCount > lastMessageCount.current

    if (hasNewMessages && !isUserScrolling.current) {
      requestAnimationFrame(() => {
        scrollToBottom('smooth')
      })
    }

    lastMessageCount.current = messageCount
  }, [messages.length, scrollToBottom])

  /**
   * 流式输出时使用 ResizeObserver 监听内容高度变化自动跟随
   */
  useEffect(() => {
    if (!isGenerating || !containerRef.current) {
      return
    }

    const container = containerRef.current
    let lastHeight = 0

    resizeObserverRef.current = new ResizeObserver((entries) => {
      for (const entry of entries) {
        const newHeight = entry.contentRect.height
        if (newHeight > lastHeight && !isUserScrolling.current) {
          lastHeight = newHeight
          scrollToBottom('auto')
        }
      }
    })

    resizeObserverRef.current.observe(container)

    return () => {
      if (resizeObserverRef.current) {
        resizeObserverRef.current.disconnect()
      }
    }
  }, [isGenerating, scrollToBottom])

  /** 清理定时器 */
  useEffect(() => {
    return () => {
      if (scrollTimeoutRef.current) {
        clearTimeout(scrollTimeoutRef.current)
      }
    }
  }, [])

  /**
   * 渲染单个消息项
   */
  const renderItem = useCallback(
    (index: number) => {
      const message = messages[index]
      const isLast = index === messages.length - 1

      return (
        <div className="group">
          <MessageItem
            message={message}
            isLast={isLast}
            isGenerating={isGenerating && isLast}
            onRegenerate={onRegenerate}
            onEdit={onEdit}
            onDelete={onDelete}
            modelName={modelName}
            searchQuery={searchQuery}
          />
        </div>
      )
    },
    [messages, isGenerating, onRegenerate, onEdit, onDelete, modelName, searchQuery],
  )

  /**
   * 首次加载完成后滚动到底部
   */
  useEffect(() => {
    if (messages.length > 0 && !initialScrollDone.current) {
      const timer = setTimeout(() => {
        if (virtuosoRef.current) {
          virtuosoRef.current.scrollToIndex({
            index: messages.length - 1,
            behavior: 'auto',
            align: 'end',
          })
          initialScrollDone.current = true
        }
      }, 100)
      return () => clearTimeout(timer)
    }
  }, [messages.length])

  /** 切换会话时重置初始滚动标记 */
  useEffect(() => {
    if (messages.length === 0) {
      initialScrollDone.current = false
    }
  }, [messages])

  /**
   * 渲染头部加载更多组件
   */
  const HeaderComponent = useCallback(() => {
    if (!hasMore && !isLoadingMore) {
      return null
    }

    return (
      <div className="flex items-center justify-center py-4">
        {isLoadingMore ? (
          <div className="text-muted-foreground flex items-center gap-2">
            <Loader2 className="h-4 w-4 animate-spin" />
            <span className="text-sm">加载历史消息...</span>
          </div>
        ) : (
          <div className="text-muted-foreground text-sm">向上滚动加载更多</div>
        )}
      </div>
    )
  }, [hasMore, isLoadingMore])

  /**
   * 安全的初始索引
   */
  const initialTopMostItemIndex = useMemo(() => {
    if (messages.length > 0) {
      return messages.length - 1
    }
    return 0
  }, [messages.length])

  /** 空状态渲染 */
  if (messages.length === 0) {
    return (
      <div
        className={`flex flex-1 items-center justify-center ${className}`}
        data-testid="message-list-empty"
      >
        <div className="text-muted-foreground text-center">
          <div className="mb-4 text-4xl">{'\uD83D\uDCAC'}</div>
          <p>开始新的对话</p>
          <p className="mt-1 text-sm">发送消息开始与 AI 助手交流</p>
        </div>
      </div>
    )
  }

  return (
    <div ref={containerRef} className={`flex-1 ${className}`} data-testid="message-list">
      <Virtuoso
        ref={virtuosoRef}
        style={{ height: '100%' }}
        data={messages}
        itemContent={renderItem}
        onScroll={(e) => {
          const target = e.target as HTMLElement
          handleScroll(target.scrollTop, true)
        }}
        initialTopMostItemIndex={initialTopMostItemIndex}
        increaseViewportBy={{ top: 100, bottom: 300 }}
        alignToBottom={true}
        followOutput={isGenerating ? 'smooth' : false}
        components={{
          Header: HeaderComponent,
          Footer: () => (
            <>
              {isGenerating && messages[messages.length - 1]?.role === 'user' && (
                <div className="flex items-start gap-3 px-4 py-3">
                  <div className="bg-primary/10 flex h-8 w-8 shrink-0 items-center justify-center rounded-full">
                    <Loader2 className="text-primary h-4 w-4 animate-spin" />
                  </div>
                  <div className="bg-secondary/50 rounded-2xl rounded-tl-sm px-4 py-2.5">
                    <span className="text-muted-foreground text-sm">思考中...</span>
                  </div>
                </div>
              )}
              <div className="h-4" />
            </>
          ),
        }}
      />
    </div>
  )
}
