/**
 * 消息列表组件（虚拟滚动版本）
 *
 * 用 react-virtuoso 实现聊天消息流的标准行为：
 *   1. 打开就在底部（initialTopMostItemIndex + 数据到达后 scrollToIndex 兜底）
 *   2. 向上滚动加载更早历史（startReached 回调，触顶时触发，防重复）
 *   3. 加载历史后视口位置不变——靠 firstItemIndex 机制：prepend N 条时
 *      store 递增 prependedCount，firstItemIndex 同步递减 N，virtuoso 自动
 *      保持当前滚动位置（新内容出现在上方，用户可继续向上滚）。
 *
 * 关键设计：firstItemIndex 由 store 权威维护（prependedCountByPipeline），
 * 组件只读取，不在组件内对比前后帧猜测 prepend——后者不可靠（用户反馈
 * 「有时能加载有时不能」的根因之一）。
 *
 * MessageItem 已用 React.memo 包裹，历史消息不随流式重渲染。
 */

import { Loader2 } from 'lucide-react'
import { ComponentProps, ComponentType, useCallback, useEffect, useMemo, useRef } from 'react'
import { Virtuoso } from 'react-virtuoso'
import type { VirtuosoHandle } from 'react-virtuoso'
import { MessageItem } from './MessageItem'
import type { MessageListProps } from './types'
import type { Message } from '@/types/models'

/**
 * firstItemIndex 的初始基准值。virtuoso 要求 firstItemIndex 为正数；
 * prepend N 条则递减 N。基准值足够大以覆盖超长会话的累计 prepend 量。
 */
const FIRST_ITEM_INDEX_BASE = 1_000_000

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
  /** 累计向上翻页插入的条数（由 store 权威维护，驱动 firstItemIndex） */
  prependedCount?: number
}

/**
 * 消息列表组件（react-virtuoso 虚拟滚动版本）
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
  taskId,
  prependedCount = 0,
}: ExtendedMessageListProps) => {
  const virtuosoRef = useRef<VirtuosoHandle>(null)

  /**
   * 是否"跟随底部"——决定流式/新内容时是否把视图钉在底部。
   * 初始 true（看最新消息）；用户主动上滑 → false（停止跟随，翻历史）；
   * 用户滚回底部附近 → true（恢复跟随）。通过驱动 followOutput 的返回值生效。
   */
  const isFollowingBottom = useRef(true)

  /** 首次数据到达后是否已执行过钉底（用于首次定位的兜底） */
  const pinnedOnFirstData = useRef(false)
  /** 上一帧渲染的消息总数，用于检测 initFromAPI 全量替换造成的内容高度突变 */
  const prevRenderedCount = useRef(0)

  /**
   * followOutput 驱动函数：内容增长时是否自动钉到底部。
   * 受 isFollowingBottom 控制——用户上滑翻历史时不抢回底部。
   * 注意：followOutput 仅在 virtuoso 判定 data 变化时触发，对「同数量但内容高度变化」
   * （如 markdown 异步渲染撑高、initFromAPI 全量替换后高度突变）不一定可靠，
   * 故另有 itemsRendered + 内容高度变化的重钉兜底（见下方）。
   */
  const followOutput: ComponentProps<typeof Virtuoso<Message>>['followOutput'] = useCallback(
    () => (isFollowingBottom.current ? 'auto' : false),
    [],
  )

  /**
   * 把视图钉到底部（仅在跟随底部时）。封装 scrollToIndex，统一入口。
   */
  const pinToBottom = useCallback(() => {
    virtuosoRef.current?.scrollToIndex({ index: 'LAST', behavior: 'auto' })
  }, [])

  /**
   * 滚到顶部时触发加载更多。virtuoso 的 startReached 在视口触顶时触发，
   * 配合 hasMore/isLoadingMore 防重复，每次只加载一页。
   */
  const startReached = useCallback(() => {
    if (hasMore && !isLoadingMore && onLoadMore) {
      onLoadMore()
    }
  }, [hasMore, isLoadingMore, onLoadMore])

  /**
   * 到达/离开底部状态变化：滚回底部附近时恢复跟随。
   */
  const atBottomStateChange = useCallback((atBottom: boolean) => {
    if (atBottom) {
      isFollowingBottom.current = true
    }
  }, [])

  /**
   * 真实手势监听：用户通过 wheel/touch 滚动时立即停止跟随底部。
   * 用挂载标记防止 virtuoso 多次调用 scrollerRef 导致重复绑定监听器。
   * 仅真实手势才算用户意图上滑，virtuoso 程序性滚动（followOutput 钉底）不触发。
   */
  const boundEl = useRef<HTMLElement | null>(null)
  const scrollerRef = useCallback((element: HTMLElement | Window | null) => {
    if (!element || !(element instanceof HTMLElement) || boundEl.current === element) return
    const markUserScroll = () => {
      isFollowingBottom.current = false
    }
    element.addEventListener('wheel', markUserScroll, { passive: true })
    element.addEventListener('touchstart', markUserScroll, { passive: true })
    boundEl.current = element
  }, [])

  /**
   * 核心定位逻辑（补回 commit 28c670a0 的冷加载重钉防护）。
   *
   * 旧自研滚动版用 ResizeObserver 监听内容容器，内容高度变化且跟随底部时即钉底，
   * 专门解决「persist 快照钉底后 initFromAPI 异步全量替换使内容高度突变，视图停在
   * 快照高度的中间」。重写为 virtuoso 后该机制被删，导致刷新后停在中间。
   *
   * 现在用 messages 引用变化检测：快照→API 全量替换会产生新数组引用（store 的 set），
   * 触发本 effect。只要仍在跟随底部，就重新钉底，覆盖流式增长 / initFromAPI 重建 /
   * markdown 异步渲染撑高三种高度突变场景。用户主动上滑（isFollowingBottom=false）时
   * 不抢回底部，尊重翻历史意图。
   */
  useEffect(() => {
    if (messages.length === 0) return
    if (!isFollowingBottom.current) return
    // 首次数据用 RAF 等待 virtuoso 完成首次测量再钉，避免测量未完成时定位失准
    if (!pinnedOnFirstData.current) {
      pinnedOnFirstData.current = true
      requestAnimationFrame(() => pinToBottom())
      prevRenderedCount.current = messages.length
      return
    }
    // 后续内容变化（含 initFromAPI 全量替换、流式追加）：跟随底部则重钉。
    // 不限制 messages.length 变化方向——initFromAPI 替换可能使条数增/减，关键是
    // 数组引用变了就说明内容已更新，跟随底部时必须重新对齐到底部最新消息。
    pinToBottom()
    prevRenderedCount.current = messages.length
  }, [messages, pinToBottom])

  /**
   * 流式结束（isGenerating true→false）且仍在跟随时钉底一次，收尾。
   */
  const prevGenerating = useRef(false)
  useEffect(() => {
    if (prevGenerating.current && !isGenerating && isFollowingBottom.current) {
      pinToBottom()
    }
    prevGenerating.current = isGenerating
  }, [isGenerating, pinToBottom])

  /** 渲染单个消息项 */
  const itemContent = useCallback(
    (_index: number, message: Message) => {
      const isLast = message.id === messages[messages.length - 1]?.id
      return (
        <div className="group">
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
    [isGenerating, modelName, searchQuery, taskId, messages],
  )

  /** Header/Footer 用 useMemo 稳定引用，避免每次渲染重建导致 virtuoso 重置内部状态 */
  const headerComponent = useMemo<ComponentType | undefined>(
    () =>
      hasMore
        ? () => (
            <div className="flex items-center justify-center py-4">
              {isLoadingMore ? (
                <>
                  <Loader2 className="text-muted-foreground h-4 w-4 animate-spin" />
                  <span className="text-muted-foreground ml-2 text-sm">加载历史消息...</span>
                </>
              ) : (
                <span className="text-muted-foreground text-sm">向上滚动加载更多</span>
              )}
            </div>
          )
        : undefined,
    [hasMore, isLoadingMore],
  )

  const lastIsUser = messages[messages.length - 1]?.role === 'user'
  const footerComponent = useMemo<ComponentType>(
    () => () => (
      <>
        {isGenerating && lastIsUser && (
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
    [isGenerating, lastIsUser],
  )

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
      className={`flex min-h-0 flex-1 flex-col ${className}`}
      data-testid="message-list"
    >
      <Virtuoso<Message>
        ref={virtuosoRef}
        data={messages}
        computeItemKey={(index) => {
          const msg = messages[index]
          return msg ? itemKey(msg) : index
        }}
        itemContent={itemContent}
        firstItemIndex={FIRST_ITEM_INDEX_BASE - prependedCount}
        initialTopMostItemIndex={Math.max(0, messages.length - 1)}
        followOutput={followOutput}
        startReached={startReached}
        atBottomStateChange={atBottomStateChange}
        scrollerRef={scrollerRef}
        style={{ height: '100%' }}
        components={{
          Header: headerComponent,
          Footer: footerComponent,
        }}
        increaseViewportBy={{ top: 600, bottom: 600 }}
        atTopThreshold={200}
        defaultItemHeight={120}
      />
    </div>
  )
}

/** 消息项稳定 key：id + sequence，避免虚拟列表测量缓存因 key 漂移失效 */
function itemKey(message: Message): string {
  return `${message.id}-${message.sequence ?? 0}`
}
