/**
 * 消息列表滚动 Hook
 *
 * 将消息列表的所有滚动逻辑从 MessageList 组件中提炼为独立 hook，
 * 从架构层面解决滚动问题（30天4次BUG-FIX）。
 *
 * 职责：
 * - 自动跟随流式输出
 * - 新消息追加时自动滚动
 * - 内容变化时（流式追加文本/工具卡片）自动跟随
 * - 历史消息加载（prepend）后不跳动
 * - 首次加载滚动到底部
 * - Tab 切换缓存/恢复滚动位置
 * - 到达顶部触发加载更多
 *
 * 暴露接口：
 * - virtuosoRef: Virtuoso 句柄
 * - containerRef: 容器 DOM ref
 * - shouldFollowOutput: 是否跟随流式输出
 * - scrollToBottom(): 手动滚动到底部
 * - onScroll: Virtuoso onScroll 回调
 * - handleStartReached: 到达顶部回调
 * - HeaderComponent: 加载更多头部组件
 * - initialTopMostItemIndex: Virtuoso 初始位置
 */

import { Loader2 } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { VirtuosoHandle } from 'react-virtuoso'

/** 每个 Tab 的滚动位置缓存 */
const scrollPositionCache = new Map<string, number>()

/** 消息条目最小接口（hook 只读 sequence 和 role） */
interface ScrollMessage {
  sequence?: number
  role: string
}

/** useMessageScroll 配置 */
export interface UseMessageScrollOptions {
  /** 消息列表 */
  messages: ScrollMessage[]
  /** 是否正在流式生成 */
  isGenerating: boolean
  /** 是否还有更多历史消息 */
  hasMore: boolean
  /** 是否正在加载更多 */
  isLoadingMore: boolean
  /** 加载更多回调 */
  onLoadMore?: () => void
  /** 当前 Tab ID，用于缓存/恢复滚动位置 */
  tabId?: string
}

/** useMessageScroll 返回值 */
export interface UseMessageScrollReturn {
  /** Virtuoso 组件 ref */
  virtuosoRef: React.RefObject<VirtuosoHandle | null>
  /** 容器 DOM ref */
  containerRef: React.RefObject<HTMLDivElement | null>
  /** 是否跟随流式输出（传给 Virtuoso followOutput） */
  shouldFollowOutput: boolean
  /** 手动滚动到底部 */
  scrollToBottom: (behavior?: 'smooth' | 'auto') => void
  /** Virtuoso onScroll 回调 */
  onScroll: (e: Event) => void
  /** 到达顶部回调（传给 Virtuoso startReached） */
  handleStartReached: () => void
  /** 头部加载更多组件 */
  HeaderComponent: () => React.ReactNode | null
  /** Virtuoso initialTopMostItemIndex */
  initialTopMostItemIndex: number | undefined
}

/**
 * 消息列表滚动 Hook
 */
export function useMessageScroll({
  messages,
  isGenerating,
  hasMore,
  isLoadingMore,
  onLoadMore,
  tabId,
}: UseMessageScrollOptions): UseMessageScrollReturn {
  const virtuosoRef = useRef<VirtuosoHandle>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const isNearBottom = useRef(true)
  const isNearTop = useRef(false)
  const lastMessageCount = useRef(messages.length)
  const lastMaxSequence = useRef(0)
  const resizeObserverRef = useRef<ResizeObserver | null>(null)
  const initialScrollDone = useRef(false)
  const lastLoadingMore = useRef(isLoadingMore)
  const prevIsGeneratingRef = useRef(false)

  /**
   * 是否跟随流式输出自动滚动到底部
   *
   * BUG-FIX-fix_20260607_follow_output_state:
   * 用 state 驱动 followOutput，用户发送消息时强制设为 true，
   * 用户主动上滚时设为 false，确保流式输出始终跟随。
   *
   * BUG-FIX-fix_20260617_scroll_rerender_loop:
   * 问题根因: onScroll 每次 scroll 事件都调用 setShouldFollowOutput(nearBottom)，
   *   即使值没变也会触发 React 重渲染 → ChatContainer 重渲染 → pipelineMessages
   *   selector 返回新数组 → Virtuoso 收到新 data 引用 → 全量重新渲染 → 滚动卡顿/重复。
   *   老会话消息少不做虚拟化所以不受影响，新会话消息多时虚拟化与重渲染冲突导致重复。
   * 修复方案: 只在 shouldFollowOutput 值真正变化时才 setState，避免无意义的重渲染。
   * 影响范围: 消息列表滚动性能和渲染稳定性
   * 修复日期: 2026-06-17
   */
  const [shouldFollowOutput, setShouldFollowOutput] = useState(true)
  const shouldFollowOutputRef = useRef(true)

  /** 滚动到底部 */
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
   * 处理滚动事件：检测是否在底部附近，同步 shouldFollowOutput state
   *
   * BUG-FIX-fix_20260617_scroll_rerender_loop:
   * 只在 shouldFollowOutput 值真正变化时才 setState，
   * 避免 scroll 事件频繁触发无意义的 React 重渲染。
   */
  const onScroll = useCallback(
    (e: Event) => {
      const target = e.target as HTMLElement
      const { scrollTop, scrollHeight, clientHeight } = target
      const distanceFromBottom = scrollHeight - scrollTop - clientHeight
      const nearBottom = distanceFromBottom <= 150
      isNearBottom.current = nearBottom
      isNearTop.current = scrollTop <= 150
      // 只在值真正变化时才 setState，避免 scroll 事件风暴导致重渲染循环
      if (shouldFollowOutputRef.current !== nearBottom) {
        shouldFollowOutputRef.current = nearBottom
        setShouldFollowOutput(nearBottom)
      }
    },
    [],
  )

  /**
   * 处理消息变化：新消息追加到底部时自动滚动
   *
   * BUG-FIX-fix_20260513_msg_not_realtime:
   * 当检测到新消息且最后一条是 user 消息时，强制重置 isNearBottom。
   *
   * BUG-FIX-fix_20260606_prepend_scroll_jump:
   * 通过比较最大 sequence 区分 prepend 和 append，
   * 只有最大 sequence 增大时（真正的新消息）才触发滚动。
   */
  useEffect(() => {
    const messageCount = messages.length
    const hasNewMessages = messageCount > lastMessageCount.current

    if (hasNewMessages) {
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
        shouldFollowOutputRef.current = true
        setShouldFollowOutput(true)
      }
      if (isNearBottom.current) {
        requestAnimationFrame(() => {
          scrollToBottom('smooth')
        })
      }
    }

    lastMessageCount.current = messageCount
  }, [messages.length, scrollToBottom, messages])

  /**
   * BUG-FIX-fix_20260507_content_change_scroll:
   * 流式输出期间，消息数量不变但内容变化时也需要滚动。
   */
  const lastMessageContentSignature = useMemo(() => {
    if (messages.length === 0) return ''
    const last = messages[messages.length - 1]
    return `${last.sequence ?? 0}-${last.role}`
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
   * BUG-FIX-REQ-29: 流式输出结束后强制滚动到底部
   *
   * 流式输出停止后，代码块语法高亮等延迟渲染可能导致内容高度变化。
   * 检测 isGenerating 从 true → false 时，执行最终滚动确保用户看到完整内容。
   */
  useEffect(() => {
    const wasGenerating = prevIsGeneratingRef.current
    prevIsGeneratingRef.current = isGenerating

    if (wasGenerating && !isGenerating && messages.length > 0) {
      // 延迟执行，等待代码块语法高亮等延迟渲染完成
      const timer = setTimeout(() => {
        scrollToBottom('smooth')
      }, 300)
      return () => clearTimeout(timer)
    }
  }, [isGenerating, messages.length, scrollToBottom])

  /**
   * 首次加载完成后滚动到底部
   *
   * BUG-FIX-fix_20260601_scroll_to_bottom_on_load:
   * 页面刷新后加载历史消息后滚动到底部。
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
   * 历史消息加载完成后滚动到底部（仅首次加载）
   *
   * BUG-FIX-fix_20260606_prepend_scroll_jump:
   * 只在首次加载时滚动到底部，向上翻页加载历史消息完成后不再跳到底部。
   */
  useEffect(() => {
    const wasLoading = lastLoadingMore.current

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
   * 组件卸载时保存当前滚动位置到缓存
   *
   * BUG-FIX-fix_20260607_tab_scroll_position:
   * 卸载前缓存第一个可见消息 index，重新挂载时恢复。
   */
  useEffect(() => {
    return () => {
      if (tabId && virtuosoRef.current) {
        try {
          const state = virtuosoRef.current.getState()
          if (state?.ranges?.length) {
            const firstVisibleIndex = state.ranges[0].startIndex
            scrollPositionCache.set(tabId, firstVisibleIndex)
          }
        } catch {
          // Virtuoso getState 可能在卸载时不可用，忽略
        }
      }
    }
  }, [tabId])

  /**
   * 处理滚动到顶部加载更多
   *
   * BUG-FIX-fix_20260529_scroll_load_more:
   * 添加 hasMore 和 isLoadingMore 守卫条件。
   */
  const handleStartReached = useCallback(() => {
    if (hasMore && !isLoadingMore && onLoadMore) {
      onLoadMore()
    }
  }, [hasMore, isLoadingMore, onLoadMore])

  /**
   * BUG-FIX-fix_20260606_start_reached_stale:
   * prepend 完成后检测用户是否仍在顶部，自动触发下一轮加载。
   */
  useEffect(() => {
    if (hasMore && !isLoadingMore && initialScrollDone.current && isNearTop.current && onLoadMore) {
      const timer = setTimeout(onLoadMore, 150)
      return () => clearTimeout(timer)
    }
  }, [hasMore, isLoadingMore, messages.length, onLoadMore])

  /** 渲染头部加载更多组件 */
  const HeaderComponent = useCallback((): React.ReactNode | null => {
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
      if (tabId) {
        const cachedIndex = scrollPositionCache.get(tabId)
        if (cachedIndex !== undefined && cachedIndex < messages.length) {
          return cachedIndex
        }
      }
      return messages.length - 1
    }
    return undefined
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages.length])

  return {
    virtuosoRef,
    containerRef,
    shouldFollowOutput,
    scrollToBottom,
    onScroll,
    handleStartReached,
    HeaderComponent,
    initialTopMostItemIndex,
  }
}
