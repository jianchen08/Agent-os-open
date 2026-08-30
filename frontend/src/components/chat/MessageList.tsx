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

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Loader2 } from '@/assets/icons'
import { cn } from '@/lib/utils'
import { mergeConsecutiveAssistantMessages } from '@/services/api/session'
import { compareMessages } from '@/utils/messageOrder'
import { MessageItem } from './MessageItem'
import type { MessageListProps } from './types'
import type { Message } from '@/types/models'

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
  /** 编辑重发回调（下传给 MessageItem） */
  onEdit?: (messageId: string, newContent: string) => Promise<void> | void
  /** 重新生成回调（最后一条 assistant 消息上「重新生成」触发） */
  onRegenerate?: () => void
  /** 回退回调（user 消息「回退」确认后触发，参数为目标 user 消息 ID） */
  onRollbackTo?: (userMessageId: string) => void
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
  onEdit,
  onRegenerate,
  onRollbackTo,
  jumpTarget,
  onJumpConsumed,
}: ExtendedMessageListProps) => {
  /**
   * 渲染用消息：先按权威时序排序，再合并连续 assistant 为一个气泡
   * （工具调用链显示为一条消息）。
   *
   * 排序兜底（[来源: deepseek-harness-rc8 chat-snapshot-builder.orderedVisible]）：
   * 渲染顺序 = sequence→timestamp→id 时序，与事件到达顺序解耦——流式事件乱序/
   * 延迟到达（通知、注入消息、跨管道交错）不会让新到达的气泡挤到错误位置。
   * store 保持原始 sequence 保证 before_sequence 翻页正常（数据层不重排、
   * 渲染层排序，彻底解耦）。
   */
  const displayMessages = useMemo(() => {
    const ordered = [...messages].sort(compareMessages)
    const merged = mergeConsecutiveAssistantMessages(ordered)
    // 渲染层跳过"已被吸收"的 tool 消息：若该 tool 消息的 toolCallId 已出现在
    // 前一个 assistant 的 tool_call part（merge 注入了结果，渲染为 ActivityCard），
    // 则独立 tool 卡片冗余，跳过。与流式渲染保持一致（流式时 tool 结果直接更新
    // assistant 的 tool_call part，无独立 tool 消息）。
    // 注意：仅在渲染层过滤，不改 store/merge 的原始数据，流式与刷新后行为一致。
    // 兜底：若 tool 消息无 toolCallId，或前一个 assistant 无对应 tool_call part，则保留渲染。
    const absorbedToolCallIds = new Set<string>()
    for (const m of merged) {
      if (m.role === 'assistant' && m.parts) {
        for (const p of m.parts) {
          if (p.type === 'tool_call' && p.callId) {
            absorbedToolCallIds.add(p.callId)
          }
        }
      }
    }
    return merged.filter((m) => {
      if (m.role !== 'tool') return true
      const tcId = m.toolCallId
      // 无 toolCallId 或未被吸收 → 保留（兜底，避免结果丢失）
      if (!tcId || !absorbedToolCallIds.has(tcId)) return true
      // 已被 assistant 的 tool_call part 吸收 → 跳过（避免与 ActivityCard 重复）
      return false
    })
  }, [messages])

  /**
   * 待定位消息的渲染项 id（displayMessages 合并后组内首条即定位锚）。
   * 定位键是 sequence：消息 id 在合并/重建中不稳定，sequence 是管道内权威序号。
   */
  const jumpMessageId = useMemo(() => {
    if (!jumpTarget) return undefined
    const target = displayMessages.find((m) => m.sequence === jumpTarget.sequence)
    return target?.id
  }, [jumpTarget, displayMessages])

  const scrollRef = useRef<HTMLDivElement>(null)
  /** 是否在底部附近（距底部 150px 内） */
  const isNearBottom = useRef(true)
  /** 是否在顶部附近（触发加载更多） */
  const isNearTop = useRef(false)
  /**
   * 是否"跟随底部"——决定流式/新内容时是否把视图钉在底部。
   * 初始 true（看最新消息）；视口上移 → false（停止跟随，翻历史，滚动条拖拽/
   * 键盘滚动同样生效）；用户滚回底部附近 → true（恢复跟随）。
   * 这是控制流式钉底的关键：用户上移必须立即停止跟随，否则滚不动。
   */
  const isFollowingBottom = useRef(true)
  /** 首次滚动是否完成 */
  const initialScrollDone = useRef(false)
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
  /** 定位跳转高亮中的消息 id（CSS 过渡淡出，无定时清除） */
  const [highlightedMessageId, setHighlightedMessageId] = useState<string | null>(null)

  /** 渲染单个消息项（index 基于 displayMessages） */
  const renderItem = useCallback(
    (message: Message, index: number, total: number) => {
      const isLast = index === total - 1
      const isHighlighted = message.id === highlightedMessageId
      return (
        <div
          className={cn('group', isHighlighted && 'message-jump-highlight')}
          data-msg-id={message.id}
          style={{
            marginBottom: index < total - 1 ? 'var(--layout-chatpanel-message-gap, 20px)' : 0,
          }}
          key={`${message.id}-${message.sequence ?? index}`}
        >
          <MessageItem
            message={message}
            isLast={isLast}
            isGenerating={isGenerating && isLast}
            modelName={modelName}
            searchQuery={searchQuery}
            taskId={taskId}
            onEdit={onEdit}
            onRegenerate={onRegenerate}
            onRollbackTo={onRollbackTo}
          />
        </div>
      )
    },
    [
      isGenerating,
      modelName,
      searchQuery,
      taskId,
      onEdit,
      onRegenerate,
      onRollbackTo,
      highlightedMessageId,
    ],
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
   * 定位跳转消息：滚动到目标消息并短暂高亮。
   *
   * 触发条件（缺一不可）：
   * - 有 jumpTarget（Sidebar 消息命中点击写入 uiStore）；
   * - 目标 sequence 已在当前已加载消息中（未加载 → 由上层 hasMore 翻页拉取后再次进入本 effect）；
   * - 当前管道非跟随底部（定位时停止钉底，避免流式/内容变化把视图拉回）。
   *
   * 滚动经 requestAnimationFrame 排队：首次定位的 RAF 钉底先执行，本定位后执行，
   * 避免被钉底覆盖。定位后置 isFollowingBottom=false，首次钉底轮询随即终止。
   */
  useEffect(() => {
    if (!jumpTarget || !jumpMessageId) return
    // 停止跟随底部：定位是用户意图，后续内容变化不得把视图拉回底部
    isFollowingBottom.current = false
    const el = scrollRef.current
    if (!el) return
    const anchor = el.querySelector<HTMLElement>(`[data-msg-id="${jumpMessageId}"]`)
    if (!anchor) return
    const targetTop = Math.max(0, anchor.offsetTop - el.clientHeight / 2)
    requestAnimationFrame(() => {
      if (scrollRef.current) {
        scrollRef.current.scrollTop = targetTop
        lastScrollTopRef.current = targetTop
      }
    })
    // 高亮标记：CSS 过渡淡出，不需要 JS 定时移除
    setHighlightedMessageId(jumpMessageId)
    // 消费完成：一次性跳转目标已定位，清除避免 Tab 来回切换重复定位
    onJumpConsumed?.()
  }, [jumpTarget, jumpMessageId, onJumpConsumed])

  /**
   * 滚动事件处理
   *
   * 用 scrollTop 方向判定用户意图——视口上移（scrollTop 变小）立即停止跟随
   * （isFollowingBottom=false）并断开钉底 observer，把控制权完全交给用户。
   * 不区分手势：滚动条拖拽/键盘滚动没有 wheel/touch 事件，同样是用户意图。
   * 不等"离开底部 150px"才停（流式期间若等过阈值才停，下一帧又会被钉底拉回，
   * 导致"滚不动"）。滚回底部附近时恢复跟随。
   *
   * 程序性上移（内容收缩引发的浏览器钳制、scroll-anchoring 视口保持）由底部
   * 恢复自愈，无需单独豁免：钳制落点必在底部，锚定保持视口原位（跟随中距底
   * ≈0），两者停下时都满足 isNearBottom，同一事件内随即恢复跟随，不会停中间。
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

      // 视口上移 → 用户翻历史，停止跟随。
      // contentResize observer 与各钉底点内部判断 isFollowingBottom，
      // 停止跟随后不再钉底。
      if (scrollTop < prevScrollTop - 1) {
        isFollowingBottom.current = false
      }
      // 用户滚回底部附近（含程序性上移的自愈落点）→ 恢复跟随
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

    const el = scrollRef.current
    if (!el) return

    const cached = tabId ? scrollTopCache.get(tabId) : undefined
    // 缓存恢复：定位到用户离开的位置。不在底部即不跟随（流式内容变化不得把
    // 视图拉回底部）；滚回底部附近经 onScroll 恢复跟随。
    if (cached !== undefined) {
      const distance = el.scrollHeight - cached - el.clientHeight
      isNearBottom.current = distance <= 150
      isFollowingBottom.current = distance <= 150
      requestAnimationFrame(() => {
        if (scrollRef.current) {
          scrollRef.current.scrollTop = cached
          lastScrollTopRef.current = cached
        }
      })
      return
    }

    // 无缓存钉底：首次定位到底部。同步钉底 + RAF 钉底双管齐下避免中间态，
    // 并启动 1.2s 轮询钉底覆盖各浏览器渲染时序差异与异步高度变化（用户上移
    // 经 onScroll 置 isFollowingBottom=false，轮询随之终止，不抢滚动）。
    pinToBottom()
    requestAnimationFrame(() => pinToBottom())
    // 持续钉底兜底（覆盖 Edge 等浏览器的渲染时序差异）
    let ticks = 0
    const intervalId = window.setInterval(() => {
      ticks++
      if (!isFollowingBottom.current) {
        window.clearInterval(intervalId)
        return
      }
      pinToBottom()
      if (ticks >= 24) window.clearInterval(intervalId) // 1.2s 后停止
    }, 50)
  }, [messages.length, tabId, pinToBottom])

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
   * 仅在已首次定位后且消息数量增加时排队钉底。跟随判定放 rAF 内：scroll 事件
   * （停止跟随）先于 rAF 执行，排队时仍在跟随、执行前用户已上滑的钉底不落盘。
   */
  useEffect(() => {
    if (initialScrollDone.current && messages.length > lastMessageCount.current) {
      requestAnimationFrame(() => {
        if (isFollowingBottom.current) pinToBottom()
      })
    }
    lastMessageCount.current = messages.length
  }, [messages.length, pinToBottom])

  /** 流式输出期间持续跟随底部（用户上移后 isFollowingBottom=false，不再钉底） */
  useEffect(() => {
    if (isGenerating) {
      requestAnimationFrame(() => {
        if (isFollowingBottom.current) pinToBottom()
      })
    }
  }, [isGenerating, messages, pinToBottom])

  /** 流式结束后钉底一次（跟随判定放定时器内：收尾窗口内用户上移则放行其位置） */
  useEffect(() => {
    if (prevGenerating.current && !isGenerating && isFollowingBottom.current) {
      const timer = setTimeout(() => {
        if (isFollowingBottom.current) pinToBottom()
      }, 300)
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
                <Loader2 className="h-icon-md w-icon-md animate-spin" />
                <span className="text-sm">加载历史消息...</span>
              </div>
            ) : (
              <div className="text-muted-foreground text-sm">向上滚动加载更多</div>
            )}
          </div>
        )}

        {/* 消息列表（渲染合并后的气泡，连续 assistant 合为一条） */}
        {displayMessages.map((message, index) =>
          renderItem(message, index, displayMessages.length),
        )}

        {/* 底部加载占位 */}
        {isGenerating && displayMessages[displayMessages.length - 1]?.role === 'user' && (
          <div className="flex items-start gap-3 px-4 py-3">
            <div className="bg-primary/10 flex h-8 w-8 shrink-0 items-center justify-center rounded-full">
              <Loader2 className="text-primary h-icon-md w-icon-md animate-spin" />
            </div>
            <div className="bg-secondary/50 rounded-2xl rounded-tl-sm px-4 py-2.5">
              <span className="text-muted-foreground text-sm">思考中...</span>
            </div>
          </div>
        )}
        <div className="h-icon-md" />
      </div>
    </div>
  )
}
