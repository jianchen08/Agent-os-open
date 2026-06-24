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
import { MessageItem } from './MessageItem'
import type { MessageListProps } from './types'

/**
 * 每个 Tab 的滚动位置缓存
 *
 * 切换 Tab 时 MessageList 因 key 变化被销毁重建（见 ChatContainer 的
 * <MessageList key={activeTabId || sessionId}>），内部 useRef 全部重置。
 * 卸载前把 scrollTop 写入这里，重新挂载时读出恢复。
 * 内存级缓存，不跨页面刷新（仅跨同会话的卸载-重建，与原设计一致）。
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
  /** 内容子容器：ResizeObserver observe 它（内容撑高才触发，滚动容器自身高度不变） */
  const contentRef = useRef<HTMLDivElement>(null)
  /**
   * 是否"跟随底部"——决定新内容/异步高度变化时是否把视图钉在底部。
   *
   * 设计意图（对齐微信/ChatGPT 等标准聊天软件）：
   *   初始 = true（点进去直接在底部看最新消息）
   *   用户主动上滑 → 置 false（停止跟随，用户在翻历史）
   *   用户滑回底部附近 → 置 true（恢复跟随）
   *   底部追加新消息（用户发消息/收到回复）→ 强制置 true（弹到最下面）
   *
   * 用 ref 驱动（不走 state），避免滚动事件风暴触发 React 重渲染。
   */
  const isFollowingBottom = useRef(true)
  const isNearTop = useRef(false)
  const initialScrollDone = useRef(false)
  const prevGenerating = useRef(false)
  /** prepend 加载更多前的滚动锚点（用于加载后恢复视口位置） */
  const prependAnchor = useRef<{ el: HTMLElement; offset: number } | null>(null)
  const lastMessageCount = useRef(messages.length)
  const lastMinSequence = useRef<number | undefined>(undefined)

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

  /** 把滚动位置钉到最底部（立即、无动画，用于校正） */
  const pinToBottom = useCallback(() => {
    const el = scrollRef.current
    if (el) {
      el.scrollTop = el.scrollHeight
    }
  }, [])

  /**
   * 滚动事件处理
   *
   * 核心职责：根据用户滚动位置更新 isFollowingBottom（跟随状态），
   * 到顶部触发加载更多。注意——这里只更新 ref，不触发重渲染。
   */
  const onScroll = useCallback((e: React.UIEvent<HTMLDivElement>) => {
    const target = e.currentTarget
    const { scrollTop, scrollHeight, clientHeight } = target
    const distanceFromBottom = scrollHeight - scrollTop - clientHeight
    // 用户滑回底部附近 → 恢复跟随；主动上滑 → 停止跟随
    isFollowingBottom.current = distanceFromBottom <= 150
    isNearTop.current = scrollTop <= 150

    // 到达顶部触发加载更多
    if (isNearTop.current && hasMore && !isLoadingMore && onLoadMore) {
      onLoadMore()
    }
  }, [hasMore, isLoadingMore, onLoadMore])

  /**
   * BUG-FIX-fix_20260624_scroll_jitter:
   * 问题根因: 原实现用 scrollTop = scrollHeight + 固定 setTimeout 猜测内容高度，
   *   但消息内 Markdown 渲染、代码高亮、图片加载都是异步的，scrollHeight 会持续
   *   增大。固定延迟设置的 scrollTop 用的是旧高度，内容继续撑高后位置就变成了
   *   "中间"，表现为反复跳动（弹到底→内容撑高→停在中间→新内容→再跳）。
   * 修复方案: 切 Tab 进入时先尝试恢复缓存位置；无缓存则默认跟随底部。
   *   真正的"钉底"交给下面的 ResizeObserver 持续校正（内容高度一变就重钉），
   *   不再依赖固定延迟猜测高度。
   */
  useEffect(() => {
    if (messages.length === 0 || initialScrollDone.current) return
    initialScrollDone.current = true

    // 有缓存（用户之前在这个 Tab 翻过历史）→ 恢复并停止跟随
    const cached = tabId ? scrollTopCache.get(tabId) : undefined
    if (cached !== undefined && scrollRef.current) {
      isFollowingBottom.current = false
      // 延迟到下一帧，确保 DOM 已按最新 messages 渲染后再定位
      requestAnimationFrame(() => {
        if (scrollRef.current) {
          scrollRef.current.scrollTop = cached
        }
      })
      return
    }

    // 无缓存 → 跟随底部，立即钉一次（后续由 ResizeObserver 持续校正）
    isFollowingBottom.current = true
    requestAnimationFrame(pinToBottom)
  }, [messages.length, tabId, pinToBottom])

  /**
   * ResizeObserver：内容高度变化时，若仍在跟随底部则重新钉底
   *
   * 这是消除"反复跳动"的关键。Markdown/代码块/图片异步渲染撑高内容时，
   * observer 触发 → 只要用户没主动上滑（isFollowingBottom=true）→ 立即重钉底部，
   * 视觉上始终稳定在底部，不会停在中间。
   *
   * 注意 observe 的是 contentRef（内容子容器）而非 scrollRef：滚动容器
   * overflow-y:auto 自身高度固定，内容撑高时它不触发；内容容器才会变高。
   */
  useEffect(() => {
    const el = contentRef.current
    if (!el) return

    const ro = new ResizeObserver(() => {
      if (isFollowingBottom.current) {
        pinToBottom()
      }
    })
    ro.observe(el)
    return () => ro.disconnect()
  }, [pinToBottom, messages.length])

  /**
   * 区分"底部追加新消息"与"顶部 prepend 历史消息"，分别处理：
   *
   * BUG-FIX-fix_20260624_prepend_jump_to_bottom:
   * 问题根因: 原逻辑只看 messages.length 增加，prepend 加载历史时 length 也会增加，
   *   导致 isFollowingBottom 被强制置 true 并钉底 → 用户向上翻历史加载更多后视图
   *   弹到最下面。正确行为应是加载完历史后停在原位（Scroll Anchoring）。
   * 修复方案: 用最小 sequence 是否变小判断 prepend（历史消息 sequence 更小）。
   *   - prepend：不做任何滚动，交给下面的锚点恢复 effect 把视口拉回原位。
   *   - 底部追加新消息：强制跟随 + 钉底（弹到最下面）。
   */
  const currentMinSeq = messages.length > 0
    ? Math.min(...messages.map((m) => m.sequence ?? 0))
    : undefined
  useEffect(() => {
    if (messages.length > lastMessageCount.current) {
      const isPrepend =
        lastMinSequence.current !== undefined &&
        currentMinSeq !== undefined &&
        currentMinSeq < lastMinSequence.current

      if (!isPrepend) {
        // 底部追加新消息 → 弹到最下面
        isFollowingBottom.current = true
        requestAnimationFrame(pinToBottom)
      }
      // prepend 情况交给 Scroll Anchoring effect 处理，这里不动 isFollowingBottom
    }
    lastMessageCount.current = messages.length
    lastMinSequence.current = currentMinSeq
  }, [messages.length, currentMinSeq, pinToBottom])

  /**
   * Scroll Anchoring：向上加载更多（prepend）时保持视口位置稳定
   *
   * 开始加载前记录"当前视口顶部第一个消息元素 + 它距视口顶的偏移"，
   * 加载完成后（isLoadingMore 从 true→false）把该锚点元素拉回原偏移位置。
   * 这样加载历史只是"把上面的内容刷出来"，视图停在原处，不跳动。
   */
  useEffect(() => {
    // isLoadingMore 刚变 true：记录锚点（加载前的视口顶部消息）
    if (isLoadingMore && scrollRef.current && contentRef.current && !prependAnchor.current) {
      const container = scrollRef.current
      const containerTop = container.getBoundingClientRect().top
      // 在内容容器里找第一个出现在视口内的消息项
      const items = contentRef.current.querySelectorAll('[data-testid="message-item"]')
      let anchorEl: HTMLElement | null = null
      for (const item of items) {
        const rect = (item as HTMLElement).getBoundingClientRect()
        if (rect.bottom >= containerTop) {
          anchorEl = item as HTMLElement
          break
        }
      }
      if (anchorEl) {
        prependAnchor.current = {
          el: anchorEl,
          offset: anchorEl.getBoundingClientRect().top - containerTop,
        }
      }
    }

    // isLoadingMore 从 true→false：加载完成，恢复锚点位置
    if (!isLoadingMore && prependAnchor.current && scrollRef.current) {
      const { el, offset } = prependAnchor.current
      const containerTop = scrollRef.current.getBoundingClientRect().top
      const elTop = el.getBoundingClientRect().top
      // 把锚点元素拉回原来的 offset 位置
      scrollRef.current.scrollTop += elTop - containerTop - offset
      // 加载历史后用户在翻历史，保持停止跟随
      isFollowingBottom.current = false
      prependAnchor.current = null
    }
  }, [isLoadingMore, messages.length])

  /** 流式输出期间持续钉底（跟随底部时） */
  useEffect(() => {
    if (isGenerating && isFollowingBottom.current) {
      requestAnimationFrame(pinToBottom)
    }
  }, [isGenerating, messages, pinToBottom])

  /** 流式结束后钉底一次（代码块语法高亮等延迟渲染完成后） */
  useEffect(() => {
    if (prevGenerating.current && !isGenerating) {
      isFollowingBottom.current = true
      const timer = setTimeout(pinToBottom, 300)
      return () => clearTimeout(timer)
    }
    prevGenerating.current = isGenerating
  }, [isGenerating, pinToBottom])

  /** 组件卸载时缓存当前滚动位置（供下次切换回来恢复）
   *
   * effect 运行时（commit 后）scrollRef.current 有效，闭包捕获 DOM 引用。
   * 不在 cleanup 里直接读 ref：卸载时 React 先 detach ref（置 null）再跑
   * passive effect cleanup，cleanup 里 scrollRef.current 已为 null。
   */
  useEffect(() => {
    const el = scrollRef.current
    return () => {
      if (tabId && el) {
        scrollTopCache.set(tabId, el.scrollTop)
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
      data-testid="message-list"
    >
      {/* 内容子容器：ResizeObserver observe 它（内容撑高才触发，滚动容器自身高度固定） */}
      <div ref={contentRef}>
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
    </div>
  )
}
