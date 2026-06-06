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
  const isNearBottom = useRef(true)
  const isNearTop = useRef(false)
  const lastMessageCount = useRef(messages.length)
  const lastMaxSequence = useRef(0)
  const containerRef = useRef<HTMLDivElement>(null)
  const resizeObserverRef = useRef<ResizeObserver | null>(null)
  const initialScrollDone = useRef(false)
  const lastLoadingMore = useRef(isLoadingMore)
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
   * 处理滚动事件：仅检测是否在底部附近
   * 顶部加载更多由 Virtuoso 的 startReached 回调处理，比 scrollTop 判断更可靠。
   */
  const handleScroll = useCallback(
    (e: Event) => {
      const target = e.target as HTMLElement
      const { scrollTop, scrollHeight, clientHeight } = target
      const distanceFromBottom = scrollHeight - scrollTop - clientHeight
      isNearBottom.current = distanceFromBottom <= 150
      isNearTop.current = scrollTop <= 150
    },
    [],
  )

  /**
   * 处理消息变化：新消息追加到底部时自动滚动到底部
   *
   * BUG-FIX-fix_20260513_msg_not_realtime:
   * 问题根因: 用户发送消息后 isUserScrolling 可能刚被 handleScroll 设为 true
   *          （滚动检测过于敏感），导致新消息不会触发自动滚动。
   * 修复方案: 当检测到新消息且最后一条是 user 消息时，强制重置 isNearBottom，
   *          确保用户发送的消息始终能滚动到底部可见。
   * 影响范围: 用户发送消息后的自动滚动行为
   * 修复日期: 2026-05-13
   *
   * BUG-FIX-fix_20260606_prepend_scroll_jump:
   * 问题根因: 向上翻页加载历史消息时，messages.length 增加（prepend），
   *          本 effect 误判为"新消息到达"并触发 scrollToBottom，
   *          导致用户从顶部被弹回底部。
   * 修复方案: 通过比较最大 sequence 区分 prepend 和 append，
   *          只有最大 sequence 增大时（真正的新消息）才触发滚动。
   */
  useEffect(() => {
    const messageCount = messages.length
    const hasNewMessages = messageCount > lastMessageCount.current

    if (hasNewMessages) {
      // 计算当前最大 sequence
      const currentMaxSeq = messages.length > 0
        ? Math.max(...messages.map((m) => m.sequence ?? 0))
        : 0
      const isAppend = currentMaxSeq > lastMaxSequence.current
      lastMaxSequence.current = currentMaxSeq

      // 只有真正的新消息追加到底部时才滚动，prepend 历史消息不触发
      if (!isAppend) {
        lastMessageCount.current = messageCount
        return
      }

      if (messages.length > 0 && messages[messages.length - 1].role === 'user') {
        isNearBottom.current = true
      }
      if (isNearBottom.current) {
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
   * 修复方案: 额外监听最后一条消息的 parts 长度和 content 长度变化，
   *          当内容增加时（如工具卡片后新增文本块），触发滚动到底部。
   */
  const lastMessageContentSignature = useMemo(() => {
    if (messages.length === 0) return ''
    const last = messages[messages.length - 1]
    const partsCount = last.parts?.length || 0
    const contentLen = last.content?.length ?? 0
    return `${partsCount}-${contentLen}`
  }, [messages])

  useEffect(() => {
    if (isGenerating && isNearBottom.current && messages.length > 0) {
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
        if (newHeight > lastHeight && isNearBottom.current) {
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
   *
   * BUG-FIX-fix_20260601_scroll_to_bottom_on_load:
   * 问题根因: 页面刷新后，MessageList 先挂载（messages 为空），然后 API 返回消息。
   *          initialScrollDone 只在 messages.length 从 0 变为 >0 时触发一次滚动。
   *          但如果消息超过 50 条，hasMoreOlder=true，Virtuoso 顶部渲染 HeaderComponent，
   *          且 initialTopMostItemIndex 只在组件首次渲染时生效，导致视图停在中间位置，
   *          用户需要手动滚动很久才能到底部。
   * 修复方案:
   *   1. 保留原有的首次加载滚动逻辑
   *   2. 新增监听 isLoadingMore 和 hasMore 变化：
   *      - 当 isLoadingMore 从 true 变为 false 时（历史消息加载完成），滚动到底部
   *      - 当 hasMore 从 true 变为 false 时（所有历史消息加载完毕），滚动到底部
   *   3. 切换会话时重置 initialScrollDone
   * 影响范围: 页面刷新后、加载历史消息后的滚动行为
   * 修复日期: 2026-06-01
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

  /**
   * 历史消息加载完成后滚动到底部
   *
   * BUG-FIX-fix_20260606_prepend_scroll_jump:
   * 只在首次加载（initialScrollDone=false）时滚动到底部，
   * 向上翻页加载历史消息完成后不再跳到底部——用户应该在原来的位置继续阅读。
   */
  useEffect(() => {
    const wasLoading = lastLoadingMore.current

    // 只在首次加载（还未完成初始滚动）且加载完成时滚动到底部
    if (wasLoading && !isLoadingMore && !initialScrollDone.current && messages.length > 0) {
      requestAnimationFrame(() => {
        scrollToBottom('auto')
      })
    }

    lastLoadingMore.current = isLoadingMore
  }, [isLoadingMore, messages.length, scrollToBottom])

  /** 切换会话时重置初始滚动标记 */
  useEffect(() => {
    if (messages.length === 0) {
      initialScrollDone.current = false
    }
  }, [messages])

  /**
   * 处理滚动到顶部加载更多
   *
   * BUG-FIX-fix_20260529_scroll_load_more:
   * 问题根因: startReached 直接绑定 onLoadMore，没有检查 hasMore 和 isLoadingMore，
   *          导致即使没有更多消息也会发起无意义的 API 请求，且在加载中时重复触发。
   * 修复方案: 包装回调，添加 hasMore 和 isLoadingMore 守卫条件。
   * 影响范围: 向上翻页加载更多消息功能
   * 修复日期: 2026-05-29
   */
  const handleStartReached = useCallback(() => {
    if (hasMore && !isLoadingMore && onLoadMore) {
      onLoadMore()
    }
  }, [hasMore, isLoadingMore, onLoadMore])

  /**
   * BUG-FIX-fix_20260606_start_reached_stale:
   * 问题根因: Virtuoso 的 startReached 只在"到达顶部"的瞬间触发一次。
   *          向上翻页加载历史消息后，如果用户仍在顶部（prepend 的内容短），
   *          startReached 不会再次触发，导致后续页无法加载。
   * 修复方案: prepend 完成后（isLoadingMore 从 true 变 false），检测用户是否仍在顶部，
   *          如果是且 hasMore=true，自动触发下一轮加载。
   */
  useEffect(() => {
    if (hasMore && !isLoadingMore && initialScrollDone.current && isNearTop.current && onLoadMore) {
      const timer = setTimeout(onLoadMore, 150)
      return () => clearTimeout(timer)
    }
  }, [hasMore, isLoadingMore, messages.length, onLoadMore])

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

  /** 首次加载时滚动到底部，后续 prepend 不应再触发 */
  const initialTopMostItemIndex = useMemo(() => {
    if (messages.length > 0 && !initialScrollDone.current) {
      return messages.length - 1
    }
    return undefined
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
          // 仅用 msg.id 作为 key，去掉 index 和 role，
          // 让 Virtuoso 在 prepend 新消息后能正确复用 DOM 节点，避免不必要的重渲染。
          return msg?.id ?? `msg-${index}`
        }}
        onScroll={handleScroll}
        startReached={handleStartReached}
        initialTopMostItemIndex={initialTopMostItemIndex}
        increaseViewportBy={{ top: 100, bottom: 300 }}
        alignToBottom={true}
        followOutput={isNearBottom.current ? 'smooth' : false}
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
