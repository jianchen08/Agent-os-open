/**
 * 消息列表组件
 *
 * 显示消息列表，支持自动滚动、分页加载和加载状态。
 *
 * BUG-FIX-fix_20260617_virtuoso_scroll_break:
 * 问题根因: Virtuoso 虚拟列表在动态高度场景下布局崩溃，滚动后只有一个气泡
 *   重复、其余消失、大片空白。原因是 Virtuoso 的 item 高度测量与 React 重渲染
 *   时序冲突，特别是在 messages 数组引用频繁变化时。
 * 修复方案: 临时弃用 Virtuoso，改用原生 div + overflow-y:auto。
 *   消息量在数百条以内时原生渲染性能足够，且无虚拟化布局风险。
 * 影响范围: 消息列表滚动和渲染稳定性
 * 修复日期: 2026-06-17
 */

import { Loader2 } from 'lucide-react'
import { useCallback, useEffect, useRef } from 'react'
import { useMessageScroll } from './hooks/useMessageScroll'
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
  /** 当前 Tab ID，用于缓存/恢复滚动位置 */
  tabId?: string
}

/**
 * 消息列表组件（原生滚动版本，无虚拟化）
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
  tabId,
}: ExtendedMessageListProps) => {
  const scrollRef = useRef<HTMLDivElement>(null)
  const isNearBottom = useRef(true)
  const isNearTop = useRef(false)
  const initialScrollDone = useRef(false)

  /** 渲染单个消息项 */
  const renderItem = useCallback(
    (message: any, index: number) => {
      const isLast = index === messages.length - 1
      return (
        <div className="group" key={`${message.id}-${message.sequence ?? index}`}>
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
    [isGenerating, modelName, searchQuery],
  )

  /** 滚动事件处理 */
  const onScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    const target = e.currentTarget
    const { scrollTop, scrollHeight, clientHeight } = target
    const distanceFromBottom = scrollHeight - scrollTop - clientHeight
    isNearBottom.current = distanceFromBottom <= 150
    isNearTop.current = scrollTop <= 150

    // 到达顶部触发加载更多
    if (isNearTop.current && hasMore && !isLoadingMore && onLoadMore) {
      onLoadMore()
    }
  }, [hasMore, isLoadingMore, onLoadMore])

  /** 首次加载滚动到底部 */
  useEffect(() => {
    if (messages.length > 0 && !initialScrollDone.current && scrollRef.current) {
      const timer = setTimeout(() => {
        if (scrollRef.current) {
          scrollRef.current.scrollTop = scrollRef.current.scrollHeight
          initialScrollDone.current = true
        }
      }, 100)
      return () => clearTimeout(timer)
    }
  }, [messages.length])

  /** 流式输出时自动滚动到底部 */
  useEffect(() => {
    if (isGenerating && isNearBottom.current && scrollRef.current) {
      requestAnimationFrame(() => {
        if (scrollRef.current) {
          scrollRef.current.scrollTop = scrollRef.current.scrollHeight
        }
      })
    }
  }, [isGenerating, messages])

  /** 流式结束后滚动到底部 */
  const prevGenerating = useRef(false)
  useEffect(() => {
    if (prevGenerating.current && !isGenerating && scrollRef.current) {
      const timer = setTimeout(() => {
        if (scrollRef.current) {
          scrollRef.current.scrollTop = scrollRef.current.scrollHeight
        }
      }, 300)
      return () => clearTimeout(timer)
    }
    prevGenerating.current = isGenerating
  }, [isGenerating])

  /** 切换会话时重置初始滚动标记 */
  useEffect(() => {
    if (messages.length === 0) {
      initialScrollDone.current = false
    }
  }, [tabId])

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
    <div
      ref={scrollRef}
      onScroll={onScroll}
      className={`min-h-0 flex-1 overflow-y-auto ${className}`}
      data-testid="message-list"
    >
      {/* 加载更多头部 */}
      {hasMore && (
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
      )}

      {/* 消息列表 */}
      {messages.map((message, index) => renderItem(message, index))}

      {/* 底部加载占位 */}
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
    </div>
  )
}
