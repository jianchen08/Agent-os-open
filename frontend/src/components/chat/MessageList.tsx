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

      if (atTop && hasMore && !isLoadingMore && onLoadMore) {
        onLoadMore()
      }
    },
    [hasMore, isLoadingMore, onLoadMore],
  )

  /**
   * 处理消息变化：新消息到达时自动滚动到底部
   *
   * BUG-FIX-fix_20260513_msg_not_realtime:
   * 问题根因: 用户发送消息后 isUserScrolling 可能刚被 handleScroll 设为 true
   *          （滚动检测过于敏感），导致新消息不会触发自动滚动。
   * 修复方案: 当检测到新消息且最后一条是 user 消息时，强制重置 isUserScrolling，
   *          确保用户发送的消息始终能滚动到底部可见。
   * 影响范围: 用户发送消息后的自动滚动行为
   * 修复日期: 2026-05-13
   */
  useEffect(() => {
    const messageCount = messages.length
    const hasNewMessages = messageCount > lastMessageCount.current

    if (hasNewMessages) {
      if (messages.length > 0 && messages[messages.length - 1].role === 'user') {
        isUserScrolling.current = false
      }
      if (!isUserScrolling.current) {
        requestAnimationFrame(() => {
          scrollToBottom('smooth')
        })
      }
    }

    lastMessageCount.current = messageCount
  }, [messages.length, scrollToBottom])

  /**
   * BUG-FIX-fix_20260507_content_change_scroll:
   * 问题根因: 流式输出期间，工具调用完成后新文本追加到最后一条消息，
   *          消息数量不变，上面只监听 messages.length，无法检测到内容变化，
   *          导致新内容渲染了但视图不滚动，用户看到 UI 卡住。
   * 修复方案: 额外监听最后一条消息的 contentBlocks 长度变化，
   *          当内容块增加时（如工具卡片后新增文本块），触发滚动到底部。
   */
  const lastMessageContentSignature = useMemo(() => {
    if (messages.length === 0) return ''
    const last = messages[messages.length - 1]
    const blockCount = last.contentBlocks?.length ?? 0
    const contentLen = last.content?.length ?? 0
    const toolCallCount = last.toolCalls?.length ?? 0
    return `${blockCount}-${contentLen}-${toolCallCount}`
  }, [messages])

  useEffect(() => {
    if (isGenerating && !isUserScrolling.current && messages.length > 0) {
      requestAnimationFrame(() => {
        scrollToBottom('auto')
      })
    }
  }, [lastMessageContentSignature, isGenerating, messages.length, scrollToBottom])

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
            modelName={modelName}
            searchQuery={searchQuery}
          />
        </div>
      )
    },
    [messages, isGenerating, modelName, searchQuery],
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
    <div ref={containerRef} className={`min-h-0 flex-1 overflow-hidden ${className}`} data-testid="message-list">
      <Virtuoso
        ref={virtuosoRef}
        style={{ height: '100%' }}
        data={messages}
        itemContent={renderItem}
        computeItemKey={(index) => {
          const msg = messages[index]
          // BUG-FIX-fix_20260513_virtuoso_key_conflict:
          // 问题根因: 仅用 msg.id-role 作为 key，合并消息的 id 可能与原始消息冲突，
          //          导致 Virtuoso 复用错误的 DOM 节点。加入 index 确保位置唯一性。
          return msg?.id ? `${msg.id}-${msg.role}-${index}` : `msg-${index}`
        }}
        onScroll={(e) => {
          const target = e.target as HTMLElement
          handleScroll(target.scrollTop, true)
        }}
        initialTopMostItemIndex={initialTopMostItemIndex}
        increaseViewportBy={{ top: 100, bottom: 300 }}
        alignToBottom={true}
        followOutput={isGenerating || messages.length > 0 ? 'smooth' : false}
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
