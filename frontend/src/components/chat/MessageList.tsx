/**
 * 消息列表组件
 *
 * 显示消息列表，支持自动滚动、分页加载和加载状态。
 *
 * 滚动职责设计（最小职责原则）：
 *   1. MessageItem 已用 React.memo 包裹（见 MessageItem.tsx）：历史消息不随流式
 *      重渲染，避免算好的 scrollTop 被新渲染冲掉导致滚动条乱跳。
 *   2. 本组件只保留最小滚动职责：
 *      - 首次进入钉底、切 Tab 缓存/恢复 scrollTop
 *      - 用户发消息/流式期间跟随底部
 *      - 到顶触发加载更多
 *   3. 向上加载更多（prepend）的不跳由浏览器原生 CSS `overflow-anchor: auto`
 *      保证（微博/Twitter 同款机制，2019 年起全浏览器支持，Electron/Tauri 100% 兼容），
 *      无需手写任何锚点逻辑。
 */

import { Loader2 } from 'lucide-react'
import { useCallback, useEffect, useLayoutEffect, useRef } from 'react'
import { logger as loggerService } from '@/utils/logger'
import { MessageItem } from './MessageItem'
import type { MessageListProps } from './types'

const logger = loggerService.module('MessageList')

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
  /** 当前 Tab 关联的任务 ID，用于工具卡片打开文件解析工作区 */
  taskId?: string
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
  taskId,
}: ExtendedMessageListProps) => {
  const scrollRef = useRef<HTMLDivElement>(null)
  /** 是否在底部附近（距底部 150px 内） */
  const isNearBottom = useRef(true)
  /** 是否在顶部附近（触发加载更多） */
  const isNearTop = useRef(false)
  /**
   * 是否"跟随底部"——决定流式/新内容时是否把视图钉在底部。
   * 初始 true（看最新消息）；用户主动上滑 → false（停止跟随，翻历史）；
   * 用户滚回底部附近 → true（恢复跟随）。
   * 这是控制流式钉底的关键：用户上滑必须立即停止跟随，否则滚不动。
   */
  const isFollowingBottom = useRef(true)
  /** 首次滚动是否完成 */
  const initialScrollDone = useRef(false)
  /**
   * 用户是否通过真实手势（wheel/touch）滚动过。
   * 用于区分"用户主动上滑"与"程序性滚动"（高度变化导致 scrollTop 变小）。
   * onScroll 对两者都会触发，但只有真实手势才算用户意图上滑。
   * 刷新恢复时 initFromAPI 重建导致高度突减，浏览器程序性滚动会触发 onScroll，
   * 若仅凭 scrollTop 方向判断会把 isFollowingBottom 误置 false → 不钉底 → 停中间。
   */
  const userScrolled = useRef(false)
  const prevGenerating = useRef(false)
  /** 上一帧消息数量，用于判断是新消息追加还是 prepend 历史 */
  const lastMessageCount = useRef(messages.length)
  /**
   * 内容容器 ref：包裹所有消息，尺寸随内容增高。
   * ResizeObserver 监听它（而非滚动容器——滚动容器 flex-1 尺寸固定，监听不到
   * 内容 scrollHeight 变化），在内容变化时重新钉底。详见 contentResize effect。
   */
  const contentRef = useRef<HTMLDivElement>(null)
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
            taskId={taskId}
          />
        </div>
      )
    },
    [isGenerating, modelName, searchQuery, taskId],
  )

  /** 把滚动位置钉到最底部 */
  const pinToBottom = useCallback(() => {
    const el = scrollRef.current
    if (el) {
      el.scrollTop = el.scrollHeight
      // 程序设置 scrollTop 不触发 onScroll，手动同步缓存用 ref
      lastScrollTopRef.current = el.scrollHeight
    }
  }, [])

  /**
   * 滚动事件处理
   *
   * 用 scrollTop 方向判断用户意图——只要用户往上滚（scrollTop 变小），
   * 立即停止跟随（isFollowingBottom=false）并断开钉底 observer，把控制权完全交给用户。
   * 不等"离开底部 150px"才停（流式期间若等过阈值才停，下一帧又会被钉底拉回，导致"滚不动"）。
   * 滚回底部附近时恢复跟随。
   */
  const onScroll = useCallback(
    (e: React.UIEvent<HTMLDivElement>) => {
      const target = e.currentTarget
      const { scrollTop, scrollHeight, clientHeight } = target
      const distanceFromBottom = scrollHeight - scrollTop - clientHeight
      const prevScrollTop = lastScrollTopRef.current
      isNearBottom.current = distanceFromBottom <= 150
      isNearTop.current = scrollTop <= 150
      // 实时记录：卸载时 DOM 内容已被 React 清空（scrollHeight=0），读 DOM 拿到的是 0
      lastScrollTopRef.current = scrollTop

      // 用户主动上滑（scrollTop 变小）→ 立即停止跟随。
      // contentResize observer 内部判断 isFollowingBottom，停止跟随后不再钉底。
      // 仅在用户通过真实手势（wheel/touch）滚动时才判定为"主动上滑"。
      // 刷新恢复时 initFromAPI 重建导致高度突减，浏览器产生程序性滚动（无手势），
      // 此时不应把 isFollowingBottom 置 false，否则后续 ResizeObserver 不钉底 → 停中间。
      if (scrollTop < prevScrollTop - 1 && userScrolled.current) {
        isFollowingBottom.current = false
      }
      // 用户滚回底部附近 → 恢复跟随
      if (isNearBottom.current) {
        isFollowingBottom.current = true
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
   * 持续校正（initFromAPI 重建等内容高度变化时重新钉底）由下方 contentResize
   * effect 负责，本 effect 只做一次性首次定位。
   */
  // 用 useLayoutEffect 而非 useEffect 做首次定位：useLayoutEffect 在 paint 前同步钉底，
  // 从根本上消除"中间态"闪烁（useEffect 在 paint 后才跑，浏览器已先把 DOM 渲染在
  // 维持上次相对位置的"中间"位置，用户会看到一帧中间态）。
  useLayoutEffect(() => {
    if (messages.length === 0 || initialScrollDone.current) return
    initialScrollDone.current = true

    const cached = tabId ? scrollTopCache.get(tabId) : undefined
    // 缓存恢复：直接定位，不需要 observer 校正（停在用户离开的位置）
    if (cached !== undefined) {
      isNearBottom.current = false
      requestAnimationFrame(() => {
        if (scrollRef.current) {
          scrollRef.current.scrollTop = cached
          lastScrollTopRef.current = cached
        }
      })
      return
    }

    // 无缓存钉底：首次定位到底部。同步钉底 + RAF 钉底双管齐下避免中间态，
    // 并启动 1.2s 轮询钉底覆盖各浏览器渲染时序差异与异步高度变化（用户上滑会置 userScrolled 跳过，不抢滚动）。
    const el = scrollRef.current
    if (!el) return
    pinToBottom()
    requestAnimationFrame(() => pinToBottom())
    // 持续钉底兜底（覆盖 Edge 等浏览器的渲染时序差异）
    // 用户在此窗口内 wheel/touch 上滑会置 userScrolled，此时停止强制钉底
    let ticks = 0
    const intervalId = window.setInterval(() => {
      ticks++
      if (userScrolled.current) {
        window.clearInterval(intervalId)
        return
      }
      pinToBottom()
      if (ticks >= 24) window.clearInterval(intervalId)  // 1.2s 后停止
    }, 50)
  }, [messages.length, tabId, pinToBottom])

  /**
   * 注册真实手势监听（wheel/touch），区分用户主动滚动与程序性滚动。
   * 只有真实手势触发时 userScrolled 才置位，onScroll 据此判断是否为用户意图上滑。
   * 程序性滚动（高度变化导致）不置位。
   */
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const markUserScroll = () => { userScrolled.current = true }
    el.addEventListener('wheel', markUserScroll, { passive: true })
    el.addEventListener('touchstart', markUserScroll, { passive: true })
    return () => {
      el.removeEventListener('wheel', markUserScroll)
      el.removeEventListener('touchstart', markUserScroll)
    }
  }, [])

  /**
   * 持续跟随底部：内容高度变化时重新钉底。
   *
   * 用独立 effect 监听【内容容器】（随消息内容增高），只要仍在跟随底部
   * （isFollowingBottom）内容一变就钉底。覆盖 initFromAPI 重建、流式增长、
   * markdown/代码块异步渲染等所有内容高度变化场景——冷加载重建后内容高度变化
   * （经 mergeConsecutiveAssistantMessages 合并、filterBlankMessages 删空白后常使
   * 条数减少或不变），仅靠「消息条数增加才钉底」的逻辑无法触发，会导致视图停在
   * 快照渲染高度的「中间」而非最新消息底部。用户上滑后 isFollowingBottom=false，
   * observer 触发也不钉底，把控制权交给用户。
   *
   * 依赖含 messages.length：messages 从空→非空时 contentRef 才挂载，需要重跑本 effect
   * 挂上 observer。之后 contentRef 持续存在，length 变化时 disconnect+observe 同一节点，
   * 开销可忽略。
   */
  useEffect(() => {
    const content = contentRef.current
    if (!content) return
    const ro = new ResizeObserver(() => {
      if (isFollowingBottom.current) {
        pinToBottom()
      }
    })
    ro.observe(content)
    return () => ro.disconnect()
  }, [pinToBottom, messages.length])

  /**
   * 底部追加新消息 → 跟随底部
   *
   * 仅在已首次定位后、消息数量增加且仍在跟随底部时钉底。
   * 用户上滑（isFollowingBottom=false）时不强行拉回。
   */
  useEffect(() => {
    if (initialScrollDone.current && messages.length > lastMessageCount.current && isFollowingBottom.current) {
      requestAnimationFrame(pinToBottom)
    }
    lastMessageCount.current = messages.length
  }, [messages.length, pinToBottom])

  /** 流式输出期间持续跟随底部（用户上滑后 isFollowingBottom=false，不再钉底） */
  useEffect(() => {
    if (isGenerating && isFollowingBottom.current) {
      requestAnimationFrame(pinToBottom)
    }
  }, [isGenerating, messages, pinToBottom])

  /** 流式结束后钉底一次（仅当仍在跟随底部时，否则用户在翻历史不打扰） */
  useEffect(() => {
    if (prevGenerating.current && !isGenerating && isFollowingBottom.current) {
      const timer = setTimeout(pinToBottom, 300)
      return () => clearTimeout(timer)
    }
    prevGenerating.current = isGenerating
  }, [isGenerating, pinToBottom])

  /**
   * 组件卸载时缓存当前滚动位置（供下次切换回来恢复）
   *
   * 读 onScroll 实时记录的 lastScrollTopRef（用户最后的真实滚动位置），而不读 DOM：
   * 切 Tab 卸载时 React 先清空消息 DOM（scrollHeight/scrollTop 归 0），再跑 cleanup，
   * 此时读 el.scrollTop 拿到的是垃圾值 0，存进缓存会导致切回时恢复到顶部。
   * effect 运行时（commit 后）闭包捕获 DOM 引用；不在 cleanup 里直接读 ref，
   * 因为卸载时 React 先 detach ref（置 null）再跑 passive effect cleanup。
   */
  useEffect(() => {
    return () => {
      if (tabId) {
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
