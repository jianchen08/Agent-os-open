/**
 * 消息列表组件
 *
 * 显示消息列表，支持自动滚动、分页加载和加载状态。
 *
 * BUG-FIX-fix_20260624_scroll_jitter_rewrite:
 * 问题根因: 此前手写了一大套滚动 effect（isFollowingBottom + ResizeObserver
 *   + Scroll Anchoring + 流式钉底...），多 effect 互相打架、时序无法稳定，
 *   叠加 MessageItem 缺 memo 导致流式期间整列全量重渲染（40-60次/秒），
 *   算好的 scrollTop 下一帧就被新渲染冲掉 → 滚动条乱跳。
 * 修复方案（路线B）:
 *   1. MessageItem 加 React.memo（见 MessageItem.tsx）：历史消息不再随流式重渲染，
 *      scrollTop 设好后不被冲掉。
 *   2. 本组件大幅删减手写滚动逻辑，只保留最小职责：
 *      - 首次进入钉底、切 Tab 缓存/恢复 scrollTop
 *      - 用户发消息/流式期间跟随底部
 *      - 到顶触发加载更多
 *   3. 向上加载更多（prepend）不跳交给浏览器原生 CSS `overflow-anchor: auto`
 *      （微博/Twitter 同款机制，2019 年起全浏览器支持，Electron/Tauri 100% 兼容），
 *      不再手写任何锚点逻辑。
 * 影响范围: 消息列表滚动稳定性、流式渲染性能
 * 修复日期: 2026-06-24
 */

import { Loader2 } from 'lucide-react'
import { useCallback, useEffect, useRef } from 'react'
import { MessageItem } from './MessageItem'
import type { MessageListProps } from './types'

/**
 * 每个 Tab 的滚动位置缓存
 *
 * 切换 Tab 时 MessageList 因 key 变化被销毁重建（见 ChatContainer 的
 * <MessageList key={activeTabId || sessionId}>），卸载前把 scrollTop 写入这里，
 * 重新挂载时读出恢复。内存级缓存，不跨页面刷新。
 */
const scrollTopCache = new Map<string, number>()

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
  /** 是否在底部附近（跟随底部） */
  const isNearBottom = useRef(true)
  /** 是否在顶部附近（触发加载更多） */
  const isNearTop = useRef(false)
  /** 首次滚动是否完成 */
  const initialScrollDone = useRef(false)
  const prevGenerating = useRef(false)
  /** 上一帧消息数量，用于判断是新消息追加还是 prepend 历史 */
  const lastMessageCount = useRef(messages.length)
  /**
   * "钉底 observer"：无缓存首次进入时挂上，持续把 scrollTop 钉在底部。
   * 用途：抵消 initFromAPI 在首次加载后异步重建 DOM 把滚动位置冲回顶部的行为。
   * 用户主动上滑（isNearBottom=false）后断开，把控制权交还用户。
   */
  const bottomObserverRef = useRef<ResizeObserver | null>(null)
  /**
   * 最近一次真实 scrollTop（onScroll 实时记录）。
   * 切 Tab 卸载时 React 会先清空消息 DOM（scrollHeight/scrollTop 归 0），
   * 此时读 DOM 拿到的是垃圾值 0；改读此 ref 拿到用户最后的真实位置。
   */
  const lastScrollTopRef = useRef(0)

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

  /** 把滚动位置钉到最底部 */
  const pinToBottom = useCallback(() => {
    const el = scrollRef.current
    if (el) {
      // eslint-disable-next-line no-console
      console.log('[scroll-debug] pinToBottom: scrollHeight=%d clientHeight=%d', el.scrollHeight, el.clientHeight)
      el.scrollTop = el.scrollHeight
      // 程序设置 scrollTop 不触发 onScroll，手动同步缓存用 ref
      lastScrollTopRef.current = el.scrollHeight
    }
  }, [])

  /**
   * 滚动事件处理
   *
   * 只更新 ref（不触发重渲染）：跟随底部状态 + 到顶触发加载更多 +
   * 实时记录 scrollTop 供切 Tab 缓存（卸载时 DOM 已被清空，读不到准确值）。
   */
  const onScroll = useCallback(
    (e: React.UIEvent<HTMLDivElement>) => {
      const target = e.currentTarget
      const { scrollTop, scrollHeight, clientHeight } = target
      const distanceFromBottom = scrollHeight - scrollTop - clientHeight
      isNearBottom.current = distanceFromBottom <= 150
      isNearTop.current = scrollTop <= 150
      // 实时记录：卸载时 DOM 内容已被 React 清空（scrollHeight=0），读 DOM 拿到的是 0
      lastScrollTopRef.current = scrollTop

      // 用户主动上滑 → 停止钉底跟随，断开 observer 把控制权交给用户
      if (!isNearBottom.current && bottomObserverRef.current) {
        bottomObserverRef.current.disconnect()
        bottomObserverRef.current = null
      }

      if (isNearTop.current && hasMore && !isLoadingMore && onLoadMore) {
        onLoadMore()
      }
    },
    [hasMore, isLoadingMore, onLoadMore],
  )

  /**
   * 首次加载：恢复缓存位置或钉到底部
   *
   * 有缓存（之前在此 Tab 翻过历史）→ 恢复并停止跟随；
   * 无缓存 → 钉到底部（看最新消息）。
   *
   * BUG-FIX-fix_20260625_switch_to_top:
   * 问题根因: 首次钉底后，loadTabMessages→initFromAPI 仍会异步重建消息数组并重渲
   *   DOM，浏览器把滚动位置重置到顶部，而首次 effect 因 initialScrollDone 已置 true
   *   不再运行 → 视图停在顶部。之前 ResizeObserver 在 2 帧稳定后断开，错过了这次重建。
   * 修复方案: 无缓存钉底时挂一个持续工作的 ResizeObserver（存入 bottomObserverRef），
   *   内容高度一变（含 initFromAPI 重建）就重新钉底；用户主动上滑时 onScroll 检测到
   *   isNearBottom=false 并断开 observer，把控制权交给用户。memo 已消除全量重渲染，
   *   observer 不会再与重渲染冲突。
   */
  useEffect(() => {
    if (messages.length === 0 || initialScrollDone.current) return
    initialScrollDone.current = true

    const cached = tabId ? scrollTopCache.get(tabId) : undefined
    // eslint-disable-next-line no-console
    console.log('[scroll-debug] 首次加载 tabId=%s messagesLen=%d cached=%s', tabId, messages.length, cached)
    // 缓存恢复：直接定位，不需要 observer 校正（停在用户离开的位置）
    if (cached !== undefined) {
      isNearBottom.current = false
      requestAnimationFrame(() => {
        if (scrollRef.current) {
          // eslint-disable-next-line no-console
          console.log('[scroll-debug] 恢复缓存 scrollTop=%d -> 设为 %d', scrollRef.current.scrollTop, cached)
          scrollRef.current.scrollTop = cached
          // 程序设置不触发 onScroll，手动同步
          lastScrollTopRef.current = cached
        }
      })
      return
    }

    // 无缓存钉底：持续 observer 校正，抵消 initFromAPI 重建 DOM 把滚动冲回顶部
    const el = scrollRef.current
    if (!el) return
    requestAnimationFrame(pinToBottom)
    // 清理上一次残留的 observer（切 Tab 重建时）
    if (bottomObserverRef.current) {
      bottomObserverRef.current.disconnect()
    }
    const ro = new ResizeObserver(() => {
      // 用户已上滑则停止钉底（onScroll 会负责断开，这里兜底）
      if (isNearBottom.current) {
        pinToBottom()
      }
    })
    bottomObserverRef.current = ro
    ro.observe(el)
    return () => {
      ro.disconnect()
      if (bottomObserverRef.current === ro) {
        bottomObserverRef.current = null
      }
    }
  }, [messages.length, tabId, pinToBottom])

  /**
   * 底部追加新消息 → 跟随底部
   *
   * 仅在已首次定位后、消息数量增加且仍在底部附近时钉底。
   * 用户上滑（isNearBottom=false）时不强行拉回；prepend 加载历史虽也增 length，
   * 但 overflow-anchor 保持视口 + 此处 isNearBottom 多为 false，不会误钉底。
   */
  useEffect(() => {
    if (initialScrollDone.current && messages.length > lastMessageCount.current && isNearBottom.current) {
      requestAnimationFrame(pinToBottom)
    }
    lastMessageCount.current = messages.length
  }, [messages.length, pinToBottom])

  /** 流式输出期间持续跟随底部 */
  useEffect(() => {
    if (isGenerating && isNearBottom.current) {
      requestAnimationFrame(pinToBottom)
    }
  }, [isGenerating, messages, pinToBottom])

  /** 流式结束后钉底一次（代码块语法高亮等延迟渲染完成后） */
  useEffect(() => {
    if (prevGenerating.current && !isGenerating) {
      const timer = setTimeout(pinToBottom, 300)
      return () => clearTimeout(timer)
    }
    prevGenerating.current = isGenerating
  }, [isGenerating, pinToBottom])

  /**
   * 组件卸载时缓存当前滚动位置（供下次切换回来恢复）
   *
   * effect 运行时（commit 后）闭包捕获 DOM 引用；不在 cleanup 里直接读 ref，
   * 因为卸载时 React 先 detach ref（置 null）再跑 passive effect cleanup。
   */
  /**
   * 组件卸载时缓存当前滚动位置（供下次切换回来恢复）
   *
   * BUG-FIX-fix_20260625_unload_stale_scrolltop:
   * 问题根因: 切 Tab 卸载时 React 先清空消息 DOM（scrollHeight/scrollTop 归 0），
   *   再跑 cleanup。此时读 el.scrollTop 拿到的是垃圾值 0，存进缓存 → 切回时恢复到顶部。
   * 修复方案: 读 onScroll 实时记录的 lastScrollTopRef（用户最后的真实滚动位置），
   *   不读已被清空的 DOM。
   */
  useEffect(() => {
    return () => {
      if (tabId) {
        // eslint-disable-next-line no-console
        console.log('[scroll-debug] 卸载缓存 tabId=%s lastScrollTop=%d', tabId, lastScrollTopRef.current)
        scrollTopCache.set(tabId, lastScrollTopRef.current)
      }
    }
  }, [tabId])

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
      style={{ overflowAnchor: 'auto' }}
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
