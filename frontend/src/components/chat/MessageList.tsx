/**
 * 消息列表组件
 *
 * 显示消息列表，支持自动滚动、分页加载和加载状态。
 *
 * 滚动职责设计（最小职责原则）：
 *   1. MessageItem 已用 React.memo 包裹（见 MessageItem.tsx）：历史消息不随流式
 *      重渲染，避免算好的 scrollTop 被新渲染冲掉导致滚动条乱跳。
 *   2. 本组件只保留最小滚动职责：
 *      - 首次进入钉底、切 Tab 缓存/恢复滚动位置（锚点行 + 距底距离双坐标）
 *      - 用户发消息/流式期间跟随底部
 *      - 到顶触发加载更多
 *      - 段视图换装（‹i/n› 分支切换）窗口挂起程序性钉底，保持视口原位
 *   3. 向上加载更多（prepend）的不跳由浏览器原生 CSS `overflow-anchor: auto`
 *      保证（微博/Twitter 同款机制，2019 年起全浏览器支持，Electron/Tauri 100% 兼容），
 *      无需手写任何锚点逻辑。
 */

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { AlertCircle, ChevronDown, Loader2, RefreshCw } from '@/assets/icons'
import { cn } from '@/lib/utils'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { toRenderableMessages } from '@/utils/messageDisplay'
import { collectSegmentSwitcherAnchors } from '@/utils/segmentAnchor'
import { readCompressionRef } from './CompressionOriginalsButton'
import { MessageItem } from './MessageItem'
import {
  MESSAGE_STYLE_METADATA_KEY,
  PluginMessageCard,
  resolveMessageStyle,
} from './PluginMessageCard'
import { SegmentOriginals } from './SegmentOriginals'
import { resolveStreamAnchorMs } from './streamAnchor'
import type { MessageListProps } from './types'
import type { Message } from '@/types/models'

/**
 * 每个 Tab 的滚动位置缓存
 *
 * 切换 Tab 时 MessageList 因 key 变化被销毁重建（见 ChatContainer 的
 * <MessageList key={activeTabId || sessionId}>），卸载前把离开时的定位点写入
 * 这里，重新挂载时读出恢复。内存级缓存，不跨页面刷新。
 *
 * 定位点 = 锚点行（视口顶压住的消息行 id + 行顶视口内偏移）+ 距底距离回退。
 * 锚点是 content-visibility 渲染窗口化（styles/render-windowing.css）下唯一
 * 可靠的恢复坐标：重进挂载时全部消息行都是新元素，contain-intrinsic-size 的
 * 「记住上次真实尺寸」随元素销毁丢失，未渲染行按 150px 占位计高，scrollHeight
 * 严重低估——按「scrollHeight - 距底距离」绝对坐标一次定位会被 max(0,…) 钳到
 * 顶部（长会话「切会话丢位置回顶」根因），锚点行定位 + 逐帧收敛校正不受
 * 占位估计影响（见 restoreByAnchor）。距底距离保留作锚点行缺失（重建/未
 * 加载）时的回退；初值 0 天然等于「钉在底部」——消息尚未加载就切走的 Tab
 * 落缓存 0，重进恢复即钉底+跟随。
 */
interface ScrollRestorePoint {
  /** 视口顶压住的消息行 id；null = 跟随底部/未采样到（按距底距离回退） */
  anchorId: string | null
  /** 锚点行顶相对滚动容器视口顶的偏移（px，可为负=行顶已滚出视口上方） */
  anchorOffset: number
  /** 离开时的距底距离（锚点缺失时的回退定位坐标，0=底部） */
  distanceFromBottom: number
}
const scrollRestoreCache = new Map<string, ScrollRestorePoint>()

/** 空锚点表（spanIndex 无段/无活跃管道时复用同一实例，避免 useMemo 每轮新建） */
const EMPTY_SEGMENT_ANCHORS: ReadonlyMap<string, number> = new Map()

/**
 * 跟随恢复阈值：距底 ≤40px 视为"在底部"（成熟配方 20~50px 取中）。滚回阈值内
 * 恢复跟随；超出即出现「回到底部」浮钮。不可放宽——旧 150px 阈值会在上滑首帧
 * （触摸动量下单帧可达 50~100px，但首帧常仍在 150px 内）把刚暂停的跟随在同一次
 * 滚动事件里恢复回去，随后的 ResizeObserver 钉底把视图拽回（真机"上滑被压回"）。
 */
const FOLLOW_RESTORE_DISTANCE_PX = 40
/**
 * 程序性钳制自愈豁免：内容收缩引发的 scrollTop 钳制落点必在距底≈0，用户上滑
 * 首帧即 >0。方向判定据此区分"钳制自愈"（不暂停跟随）与"用户上滑"（暂停）。
 */
const CLAMP_LANDING_EPSILON_PX = 2

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
  className = '',
  hasMore = false,
  isLoadingMore = false,
  onLoadMore,
  searchQuery,
  tabId,
  taskId,
  sessionId,
  onEdit,
  onRegenerate,
  onRollbackTo,
  jumpTarget,
  onJumpConsumed,
}: ExtendedMessageListProps) => {
  /**
   * 渲染用消息：先按权威时序排序，再合并连续 assistant 为一个气泡
   * （工具调用链显示为一条消息），流水线单一真值在 utils/messageDisplay
   * （与压缩段原文还原共用，保证「段成员显示与压缩前一致」）。
   *
   * 排序兜底（[来源: deepseek-harness-rc8 chat-snapshot-builder.orderedVisible]）：
   * 渲染顺序 = sequence→timestamp→id 时序，与事件到达顺序解耦——流式事件乱序/
   * 延迟到达（通知、注入消息、跨管道交错）不会让新到达的气泡挤到错误位置。
   * store 保持原始 sequence 保证 before_sequence 翻页正常（数据层不重排、
   * 渲染层排序，彻底解耦）。
   */
  const displayMessages = useMemo(
    () => toRenderableMessages(messages),
    [messages],
  )

  /**
   * 压缩块按段归组（消息段模型 §5.1 展示层）：segmentId → 块消息组。
   * 判据纯数据面：块消息自带 compression_ref.segment_id（写入方契约），
   * 不依赖段清单拉取成败——清单失败时小条照常归组，仅原文取不到。
   */
  const compressionGroups = useMemo(() => {
    const groups = new Map<string, Message[]>()
    for (const m of displayMessages) {
      const ref = readCompressionRef(m)
      if (!ref) continue
      const group = groups.get(ref.segmentId)
      if (group) group.push(m)
      else groups.set(ref.segmentId, [m])
    }
    return groups
  }, [displayMessages])

  /** 段成员原文行：冻结副本只读——UI id 无服务端落点，操作钮不给入口 */
  const renderMemberRow = useCallback(
    (message: Message) => (
      <div className="message-render-window" data-msg-id={message.id}>
        <MessageItem
          message={message}
          isLast={false}
          searchQuery={searchQuery}
          taskId={taskId}
          readOnly
        />
      </div>
    ),
    [searchQuery, taskId],
  )

  /**
   * 待定位消息的渲染项 id（displayMessages 合并后组内首条即定位锚）。
   * 定位键是 sequence：消息 id 在合并/重建中不稳定，sequence 是管道内权威序号。
   */
  const jumpMessageId = useMemo(() => {
    if (!jumpTarget) return undefined
    const target = displayMessages.find((m) => m.sequence === jumpTarget.sequence)
    return target?.id
  }, [jumpTarget, displayMessages])

  /** 消息段模型：活跃管道的段索引（spanIndex 低频变更，订阅开销可忽略） */
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)
  const spanIndex = usePipelineMessageStore((s) => s.spanIndex)
  /** 当前管道消息加载失败标记（X1-F1）：会话切换加载失败时消息区渲染错误态 */
  const activePipelineLoadError = usePipelineMessageStore((s) =>
    activePipelineId ? (s.loadErrorByPipeline[activePipelineId] ?? false) : false,
  )
  /** 当前管道首屏消息加载中（会话切换 GET await 窗口）：渲染骨架而非空态文案 */
  const activePipelineInitialLoading = usePipelineMessageStore((s) =>
    activePipelineId ? (s.initialLoadingByPipeline[activePipelineId] ?? false) : false,
  )
  /** 本 Tab 子管道消息加载中（agentTabStore.tabMessagesLoading 的唯一 UI 消费点）：
   *  子 Tab 首屏 GET 期间同样骨架，不闪「开始新的对话」空态 */
  const tabMessagesLoading = useAgentTabStore((s) =>
    tabId ? (s.tabMessagesLoading[tabId] ?? false) : false,
  )
  /** 「重新加载」：复用统一加载入口 loadPipelineMessages（mode 默认 auto）；
   *  成功后失败标记由其成功路径清除，错误态随现有状态流自然消失。 */
  const handleRetryLoad = useCallback(() => {
    if (!activePipelineId || !sessionId) return
    void usePipelineMessageStore
      .getState()
      .loadPipelineMessages(activePipelineId, { threadId: sessionId })
  }, [activePipelineId, sessionId])
  /** 本管道的乐观段视图标记（‹i/n› 换装窗口判定，见 segmentSwapHoldRef） */
  const segmentView = usePipelineMessageStore((s) =>
    activePipelineId ? s.segmentViewByPipeline[activePipelineId] : undefined,
  )

  /**
   * ‹i/n› 多代切换器锚点（messageId → 锚点 base_seq）：每个替换点只标其覆盖
   * 范围内序列最晚的 assistant（轮末条），且锚点段数 >1 才显示（多代可切换）。
   */
  const segmentSwitcherAnchors = useMemo(() => {
    const segments = activePipelineId ? spanIndex[activePipelineId] : undefined
    if (!segments || segments.length === 0) return EMPTY_SEGMENT_ANCHORS
    return collectSegmentSwitcherAnchors(displayMessages, segments)
  }, [displayMessages, spanIndex, activePipelineId])

  /**
   * 段清单随列表挂载刷新（分支切换器的数据源）。
   *
   * 段清单此前只有两处拉取：loadPipelineMessages 与段激活 ack。boot 恢复路径
   * 两处都不执行——消息从 IndexedDB 缓存恢复（桶非空 → initSessionTabs 的
   * 「桶非空即不加载」判定跳过 loadPipelineMessages；且 restoreActiveSession
   * 因「持久化选中已有效」直接返回，不走 setActiveSession）→ 段清单永不拉 →
   * ‹i/n› 切换器不渲染（用户实证：重试多次 + F5 仍无切换按钮；装机包实证
   * fetchSegments 仅两处调用点）。
   *
   * 本列表是「该管道内容正在显示」的边界——在此拉清单，覆盖全部显示路径
   * （boot 恢复 / 切 Tab / 切会话）。非阻塞、失败降级为空清单（fetchSegments
   * 内部），不影响消息主链路。
   */
  useEffect(() => {
    if (!activePipelineId) return
    void usePipelineMessageStore.getState().fetchSegments(activePipelineId)
  }, [activePipelineId])

  const scrollRef = useRef<HTMLDivElement>(null)
  /** 是否进入顶部预载区（距顶一个视口内，提前触发加载更早消息） */
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
   * 最近一次真实 scrollTop（onScroll 实时记录），供 onScroll 方向判定做上一帧参照。
   * 切 Tab 卸载时 React 会先清空消息 DOM（scrollHeight/scrollTop 归 0），
   * 此时读 DOM 拿到的是垃圾值 0；程序化设置 scrollTop 不触发 onScroll 的环境
   * （jsdom）下也需手动同步此 ref。
   */
  const lastScrollTopRef = useRef(0)
  /**
   * 最近一次滚动的距底距离（onScroll 实时记录），卸载时写入 scrollRestoreCache。
   * 与 lastScrollTopRef 同步更新：0=钉在底部，未定位时的初值即合法的"底部"语义
   * （见 scrollRestoreCache 注释）。
   */
  const lastDistanceFromBottomRef = useRef(0)
  /**
   * 最近一次采样的滚动锚点行（翻历史时 onScroll 经 rAF 节流采样，跟随底部时
   * 置 null），卸载时并入 scrollRestoreCache，重进时锚点恢复的定位坐标。
   */
  const lastScrollAnchorRef = useRef<{ id: string; offset: number } | null>(null)
  /** 锚点采样 rAF 排队标记（一帧至多一次 DOM 扫描） */
  const anchorCaptureQueuedRef = useRef(false)
  /**
   * 段视图换装（‹i/n› 分支切换）窗口内挂起程序性钉底：换装把 base_seq 之后的
   * 消息整段换 id 重挂载，content-visibility 新行按 150px 占位计高、旧代真实
   * 高度被替换，视口四周内容高度剧变；此时钉底会把视口拽离原位（切分支滚动
   * 跳动根因）。基座 = base_seq 之前的前缀行 React 元素不动（同 key 同节点），
   * 绝对 scrollTop 不变 = 视口内容零位移。用户滚动事件即解除（交还控制权）。
   */
  const segmentSwapHoldRef = useRef(false)
  /**
   * 锚点恢复收敛循环进行中：挂起全部程序性钉底（循环自身的程序性滚动会触发
   * scroll 事件与内容高度变化，恢复期钉底会与校正互相拉扯）。
   */
  const anchorRestoreActiveRef = useRef(false)
  /** 定位跳转高亮中的消息 id（CSS 过渡淡出，无定时清除） */
  const [highlightedMessageId, setHighlightedMessageId] = useState<string | null>(null)
  /** 「回到底部」浮钮可见性：距底超出恢复阈值即出现（跟随暂停的可见反馈与一键回底） */
  const [showScrollToBottom, setShowScrollToBottom] = useState(false)
  /** 「回到底部」平滑滚动进行中：抑制浮钮中途复现（到底/用户上滑打断即清除） */
  const scrollToBottomAnimRef = useRef(false)

  /** 渲染单个消息项（index 基于 displayMessages） */
  const renderItem = useCallback(
    (message: Message, index: number, total: number) => {
      const isLast = index === total - 1
      const isHighlighted = message.id === highlightedMessageId
      // 流式总耗时锚点（2026-11-06 用户裁定）：仅流式尾消息解析（O(1) 门槛，
      // 非尾消息不付 O(n) 前扫），值 = 触发消息发出时刻，圈消失计时同葬
      const isStreamingTail = isGenerating && isLast && message.role === 'assistant'
      const streamAnchorMs = isStreamingTail
        ? resolveStreamAnchorMs(displayMessages, message.id)
        : null
      return (
        <div
          className={cn(
            'group',
            // 渲染窗口化：视口外消息跳过 Blink 布局/绘制（styles/render-windowing.css），
            // DOM 保留——滚动跟随/锚点/定位跳转模型零改动
            isHighlighted && 'message-jump-highlight',
            'message-render-window',
          )}
          data-msg-id={message.id}
          style={{
            marginBottom: index < total - 1 ? 'var(--layout-chatpanel-message-gap, 6px)' : 0,
          }}
          // key 只用消息 id（store 按 id 键控唯一）：不拼 sequence/index——流式
          // 占位的 seq 挂空、权威值后到，拼进 key 会在 seq 落地时整泡卸载重挂
          // （内容闪一下）；index 拼接同理在前插时重挂既有气泡。
          key={message.id}
        >
          <MessageItem
            message={message}
            isLast={isLast}
            isGenerating={isGenerating && isLast}
            searchQuery={searchQuery}
            taskId={taskId}
            onEdit={onEdit}
            onRegenerate={onRegenerate}
            onRollbackTo={onRollbackTo}
            segmentSwitcherBaseSeq={segmentSwitcherAnchors.get(message.id) ?? null}
            streamAnchorMs={streamAnchorMs}
          />
        </div>
      )
    },
    [
      isGenerating,
      searchQuery,
      taskId,
      onEdit,
      onRegenerate,
      onRollbackTo,
      highlightedMessageId,
      segmentSwitcherAnchors,
      displayMessages,
    ],
  )

  /**
   * 渲染行：压缩块消息按段归组——段首块槽位原位渲染「段成员原文 + 压缩
   * 分界卡」（2026-10-17 用户裁定两则）：原文与压缩前显示一致；分界卡落位
   * =被压区间消息之后、未压缩消息之前（压缩/未压缩的分界线），同段其余
   * 块跳过（由一支卡统一代表）；普通消息原样渲染。声明缺失（registry 无
   * 此样式）整组回退普通渲染——与消息卡「声明禁用同源消失」语义同型。
   * 依赖 renderItem，故置于其声明之后。
   */
  const renderedRows = useMemo(() => {
    const emitted = new Set<string>()
    const rows: ReactNode[] = []
    displayMessages.forEach((message, index) => {
      const ref = readCompressionRef(message)
      const styleId = message.metadata?.[MESSAGE_STYLE_METADATA_KEY]
      const style =
        ref && typeof styleId === 'string' ? resolveMessageStyle(styleId) : null
      if (!ref || !style) {
        rows.push(renderItem(message, index, displayMessages.length))
        return
      }
      if (emitted.has(ref.segmentId)) return
      emitted.add(ref.segmentId)
      const blocks = compressionGroups.get(ref.segmentId) ?? [message]
      rows.push(
        <div
          key={`compression-segment:${ref.segmentId}`}
          className="group"
          style={{ marginBottom: 'var(--layout-chatpanel-message-gap, 6px)' }}
        >
          {/* 被压原文还原（宿主装配面）：段取数 + 只读行挂载，在分界卡上方 */}
          <SegmentOriginals
            segmentId={ref.segmentId}
            sessionId={blocks[0]?.sessionId ?? ''}
            renderMember={renderMemberRow}
          />
          {/* 卡面（渲染本体）归插件：同段块内容由宿主路由合并为一份下行载荷，
              卡内解析 <compressed>/<current_state> 两标签；fit=auto 折叠成
              一行分界条、展开随内容长高（webview.resize 通用上行桥） */}
          <PluginMessageCard
            instanceKey={`compression-segment:${ref.segmentId}`}
            styleId={styleId as string}
            fit="auto"
            message={{
              content: blocks.map((b) => String(b.content ?? '')).join('\n'),
              metadata: blocks[0].metadata,
            }}
          />
        </div>,
      )
    })
    return rows
  }, [displayMessages, compressionGroups, renderMemberRow, renderItem])

  /** 把滚动位置钉到最底部 */
  const pinToBottom = useCallback(() => {
    const el = scrollRef.current
    if (el) {
      el.scrollTop = el.scrollHeight
      // 赋值 scrollHeight 依赖浏览器钳制到实际最大值（内容含 content-visibility
      // 占位高时目标值超出范围），ref 必须记录钳制后的实际落点：若记录幻影
      // scrollHeight，随之而来的 scroll 事件（携带钳制落点）会被下方方向判定
      // 误读为"视口上移"而停止跟随——移动端 WebView 占位收敛晚，事件派发时
      // 距底常已超出底部自愈阈值，跟随整轮失效（真机"发送后不下滚"根因）。
      lastScrollTopRef.current = el.scrollTop
      lastDistanceFromBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight
      // 钉底即无翻历史锚点（卸载缓存按距底 0=底部语义恢复）
      lastScrollAnchorRef.current = null
    }
  }, [])

  /**
   * 锚点采样：视口顶压住的消息行（第一行 bottom 越过容器顶的行）id + 行顶
   * 视口内偏移。跟随底部时无翻历史锚点（置 null，卸载缓存按距底语义恢复）。
   * 行 rect 读的是 content-visibility 占位几何也无妨——锚点只需行身份与相对
   * 偏移，恢复端逐帧校正会收敛到真实几何。
   */
  const captureScrollAnchor = useCallback(() => {
    anchorCaptureQueuedRef.current = false
    const el = scrollRef.current
    const content = contentRef.current
    if (!el || !content || isFollowingBottom.current) {
      lastScrollAnchorRef.current = null
      return
    }
    const containerTop = el.getBoundingClientRect().top
    for (const row of content.children) {
      if (!(row instanceof HTMLElement) || !row.dataset.msgId) continue
      const rect = row.getBoundingClientRect()
      if (rect.bottom > containerTop) {
        lastScrollAnchorRef.current = { id: row.dataset.msgId, offset: rect.top - containerTop }
        return
      }
    }
    lastScrollAnchorRef.current = null
  }, [])

  /** 锚点采样的 rAF 节流入口（onScroll 高频，扫描一帧至多一次） */
  const scheduleAnchorCapture = useCallback(() => {
    if (anchorCaptureQueuedRef.current) return
    anchorCaptureQueuedRef.current = true
    requestAnimationFrame(captureScrollAnchor)
  }, [captureScrollAnchor])

  /**
   * 锚点恢复：定位到缓存的锚点行并逐帧收敛校正。
   *
   * 重进挂载时消息行全是新元素，scrollHeight 是 contain-intrinsic-size 的 150px
   * 占位估计（严重低估），任何按 scrollHeight 的绝对坐标一次定位都会失准甚至被
   * 钳到顶部。这里只做粗定位后每帧量测锚点行的真实视口偏移并校正 scrollTop：
   * 随 content-visibility 渲染窗口在定位点附近铺开真实高度，偏移收敛（连续 2 帧
   * |Δ|≤1px）即按最终几何裁定跟随态；30 帧上限兜底异常环境。锚点行已不存在
   * （重建/压缩/删除）则放弃校正保留现状，由调用方回退距底恢复。
   */
  const restoreByAnchor = useCallback((anchor: { id: string; offset: number }) => {
    anchorRestoreActiveRef.current = true
    let frames = 0
    let stableFrames = 0
    const step = () => {
      const el = scrollRef.current
      if (!el || !anchorRestoreActiveRef.current) return
      const row = el.querySelector<HTMLElement>(`[data-msg-id="${anchor.id}"]`)
      if (!row) {
        anchorRestoreActiveRef.current = false
        return
      }
      const delta = row.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.offset
      if (Math.abs(delta) <= 1) {
        stableFrames++
        if (stableFrames >= 2) {
          lastScrollTopRef.current = el.scrollTop
          lastDistanceFromBottomRef.current = el.scrollHeight - el.scrollTop - el.clientHeight
          isFollowingBottom.current =
            lastDistanceFromBottomRef.current <= FOLLOW_RESTORE_DISTANCE_PX
          // 收敛即固化锚点：恢复循环自身的程序性滚动会触发 rAF 采样（恢复期
          // isFollowingBottom 仍为初始 true，采样会置空锚点），随后未滚动就切走
          // 时缓存仍锚定恢复行
          lastScrollAnchorRef.current = { id: anchor.id, offset: anchor.offset }
          anchorRestoreActiveRef.current = false
          return
        }
      } else {
        stableFrames = 0
        el.scrollTop = el.scrollTop + delta
        lastScrollTopRef.current = el.scrollTop
      }
      frames++
      if (frames < 30) requestAnimationFrame(step)
      else anchorRestoreActiveRef.current = false
    }
    requestAnimationFrame(step)
  }, [])

  /** 「回到底部」浮钮动作：恢复跟随 + 平滑滚到最底（成熟配方B标准三件套） */
  const handleScrollToBottom = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    isFollowingBottom.current = true
    scrollToBottomAnimRef.current = true
    setShowScrollToBottom(false)
    if (typeof el.scrollTo === 'function') {
      el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
    } else {
      // 环境无 element.scrollTo（老 WebView/jsdom）：直接钉底
      el.scrollTop = el.scrollHeight
    }
    // 平滑滚动受阻兜底（渲染帧挂起/平滑滚动失效的环境动画不推进）：
    // 800ms 后仍未到底则直接钉底；用户中途上滑（跟随已暂停）则放行其位置
    window.setTimeout(() => {
      const cur = scrollRef.current
      if (!cur) return
      scrollToBottomAnimRef.current = false
      if (
        isFollowingBottom.current &&
        cur.scrollHeight - cur.scrollTop - cur.clientHeight > FOLLOW_RESTORE_DISTANCE_PX
      ) {
        cur.scrollTop = cur.scrollHeight
      }
    }, 800)
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
      const cur = scrollRef.current
      if (cur) {
        cur.scrollTop = targetTop
        lastScrollTopRef.current = targetTop
        lastDistanceFromBottomRef.current = cur.scrollHeight - targetTop - cur.clientHeight
        // 定位即翻历史锚点：随后未滚动就切走，缓存也锚定目标行
        lastScrollAnchorRef.current = { id: jumpMessageId, offset: anchor.offsetTop - targetTop }
      }
    })
    // 高亮标记：CSS 过渡淡出，不需要 JS 定时移除
    setHighlightedMessageId(jumpMessageId)
    // 消费完成：一次性跳转目标已定位，清除避免 Tab 来回切换重复定位
    onJumpConsumed?.()
  }, [jumpTarget, jumpMessageId, onJumpConsumed])

  /**
   * 滚动事件处理——方向优先仲裁（成熟配方B）
   *
   * 上移（scrollTop 变小）且已离开底部钳制落点（距底 > 2px）= 用户翻历史，
   * 立即暂停跟随，且同一事件内不得被距底阈值恢复——触摸动量下上滑首帧常仍在
   * 底部阈值区内，同事件恢复会让随后的 ResizeObserver 钉底（渲染窗口化随滚动
   * 改高/流式增长）把视图拽回，形成"上滑被压回"的拉锯。
   * 非上移（下移/原位）落回恢复阈值内 → 恢复跟随。程序性上移（内容收缩引发的
   * 浏览器钳制）落点必在距底≈0，以 CLAMP_LANDING_EPSILON_PX 豁免，跟随自愈。
   * 滚动条拖拽/键盘滚动没有 wheel/touch 事件，方向判定同样成立（同为用户意图）。
   */
  const onScroll = useCallback(
    (e: React.UIEvent<HTMLDivElement>) => {
      const target = e.currentTarget
      const { scrollTop, scrollHeight, clientHeight } = target
      // 卸载拆除期幽灵事件防御：React 清空消息 DOM 的瞬间浏览器钳制 scrollTop
      // 会派发 scroll 事件（scrollHeight/clientHeight=0），读到的几何全是垃圾，
      // 写入 ref 即污染卸载缓存（重进恢复到垃圾坐标）。正常挂载下 scrollHeight
      // 恒 >0（消息列表渲染前提 + 容器 flex-1 有高），0 只能是拆除中。
      if (scrollHeight === 0 || clientHeight === 0) return
      const distanceFromBottom = scrollHeight - scrollTop - clientHeight
      const prevScrollTop = lastScrollTopRef.current
      // 顶部预载区 = 距顶一个视口内：提前加载更早消息而非等撞到顶，连续上翻不撞墙
      isNearTop.current = scrollTop <= clientHeight
      // 实时记录：卸载时 DOM 内容已被 React 清空（scrollHeight=0），读 DOM 拿到的是 0
      lastScrollTopRef.current = scrollTop
      lastDistanceFromBottomRef.current = distanceFromBottom
      // 用户滚动 = 交还控制权：换装挂起窗口立即解除
      segmentSwapHoldRef.current = false
      // 翻历史位置采样（rAF 节流；跟随态采样函数自行置空锚点）
      scheduleAnchorCapture()

      if (scrollTop < prevScrollTop - 1) {
        if (distanceFromBottom > CLAMP_LANDING_EPSILON_PX) {
          isFollowingBottom.current = false
          scrollToBottomAnimRef.current = false // 用户上滑打断回底动画，交还控制权
        }
      } else if (distanceFromBottom <= FOLLOW_RESTORE_DISTANCE_PX) {
        isFollowingBottom.current = true
        scrollToBottomAnimRef.current = false // 已到底，回底动画结束
      }
      // 「回到底部」浮钮跟随距底距离显隐（回底动画期间不复现；React 同值 setState
      // 自动去重，零多余渲染）
      setShowScrollToBottom(
        distanceFromBottom > FOLLOW_RESTORE_DISTANCE_PX && !scrollToBottomAnimRef.current,
      )

      if (isNearTop.current && hasMore && !isLoadingMore && onLoadMore) {
        onLoadMore()
      }
    },
    [hasMore, isLoadingMore, onLoadMore, scheduleAnchorCapture],
  )

  /**
   * 预载链式续载：一批更早消息落位（isLoadingMore true→false 边沿）后，若用户仍在
   * 预载区内且还有更多，自动续载下一批。覆盖"甩到顶后停住"场景——停在顶上没有后续
   * scroll 事件，仅靠 onScroll 触发只能拉到一批。守卫条数增长：空批（has_more=true
   * 但 0 条）不续载，防死循环；被 scroll-anchoring 推出预载区则不续载，回到区内由
   * onScroll 自然再触发。
   */
  const prevIsLoadingMore = useRef(false)
  const lastChainLoadCount = useRef(0)
  useEffect(() => {
    const loadCompleted = prevIsLoadingMore.current && !isLoadingMore
    prevIsLoadingMore.current = isLoadingMore
    if (
      loadCompleted &&
      hasMore &&
      isNearTop.current &&
      messages.length > lastChainLoadCount.current
    ) {
      lastChainLoadCount.current = messages.length
      onLoadMore?.()
    }
  }, [isLoadingMore, hasMore, messages.length, onLoadMore])

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

    const cached = tabId ? scrollRestoreCache.get(tabId) : undefined
    // 缓存恢复：优先锚点行定位（content-visibility 占位下唯一可靠坐标，逐帧
    // 收敛校正，见 restoreByAnchor）；锚点缺失回退距底距离（cached 是离开时的
    // 距底距离，0=钉在底部）。距底定位需要实时 scrollHeight，放 rAF 内按已布局
    // DOM 计算——注意该路径在占位估计下会失准（长会话被钳到顶），仅作回退。
    if (cached !== undefined) {
      requestAnimationFrame(() => {
        const el = scrollRef.current
        if (!el) return
        if (cached.anchorId && el.querySelector(`[data-msg-id="${cached.anchorId}"]`)) {
          restoreByAnchor({ id: cached.anchorId, offset: cached.anchorOffset })
          return
        }
        const targetTop = Math.max(0, el.scrollHeight - el.clientHeight - cached.distanceFromBottom)
        el.scrollTop = targetTop
        lastScrollTopRef.current = targetTop
        const distance = el.scrollHeight - targetTop - el.clientHeight
        lastDistanceFromBottomRef.current = distance
        isFollowingBottom.current = distance <= FOLLOW_RESTORE_DISTANCE_PX
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
    // 卸载即清：deps 含 messages.length，流式期间每次长度变化都会新建一个
    // interval，无清理会在卸载/重建风暴下叠加并发操作已卸载组件。
    return () => {
      window.clearInterval(intervalId)
    }
  }, [messages.length, tabId, pinToBottom, restoreByAnchor])

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
      // 换装窗口/锚点恢复期内不钉底：内容高度剧烈变化是换装/收敛的固有现象，
      // 此时钉底会把视口拽离原位（切分支跳动/恢复拉扯根因），控制权在用户或
      // 恢复循环手里。
      if (segmentSwapHoldRef.current || anchorRestoreActiveRef.current) return
      if (isFollowingBottom.current) {
        pinToBottom()
      }
    })
    ro.observe(content)
    return () => ro.disconnect()
  }, [pinToBottom, messages.length])

  /**
   * 段视图换装窗口：乐观换装标记在场 = 后缀消息整段换 id 重挂载进行中，挂起
   * 全部程序性钉底（视口保持绝对 scrollTop 不动 = 前缀行不动的零位移语义）。
   * 标记清除（segment_activated 对账完成，键稳定无重挂载）后双 rAF 解除，躲过
   * 清除同帧的 observer 回调；用户滚动事件在 onScroll 内即时解除。
   */
  useLayoutEffect(() => {
    if (segmentView) {
      segmentSwapHoldRef.current = true
      return
    }
    let cancelled = false
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        if (!cancelled) segmentSwapHoldRef.current = false
      })
    })
    return () => {
      cancelled = true
    }
  }, [segmentView])

  /**
   * 底部追加新消息 → 跟随底部
   *
   * 仅在已首次定位后且消息数量增加时排队钉底。跟随判定放 rAF 内：scroll 事件
   * （停止跟随）先于 rAF 执行，排队时仍在跟随、执行前用户已上滑的钉底不落盘。
   *
   * 例外：用户主动发送（乐观 user 气泡以 status='sending' 落列；触发器/HTTP
   * 注入的 user 气泡是权威消息 status='completed'，得以区分）= 回底意图，
   * 强制钉底并恢复跟随——此前翻历史的位置不保留，与主流聊天应用一致。
   */
  useEffect(() => {
    if (initialScrollDone.current && messages.length > lastMessageCount.current) {
      const lastMessage = messages[messages.length - 1]
      const isUserSend =
        lastMessage?.role === 'user' && lastMessage?.status === 'sending'
      requestAnimationFrame(() => {
        // 换装窗口/锚点恢复期不追新钉底（同 ResizeObserver 守卫口径）
        if (segmentSwapHoldRef.current || anchorRestoreActiveRef.current) return
        if (isUserSend) isFollowingBottom.current = true
        if (isFollowingBottom.current) pinToBottom()
      })
    }
    lastMessageCount.current = messages.length
  }, [messages, pinToBottom])

  /** 流式输出期间持续跟随底部（用户上移后 isFollowingBottom=false，不再钉底） */
  useEffect(() => {
    if (isGenerating) {
      requestAnimationFrame(() => {
        // 换装窗口/锚点恢复期不钉底（同 ResizeObserver 守卫口径）
        if (segmentSwapHoldRef.current || anchorRestoreActiveRef.current) return
        if (isFollowingBottom.current) pinToBottom()
      })
    }
  }, [isGenerating, messages, pinToBottom])

  /** 流式结束后钉底一次（跟随判定放定时器内：收尾窗口内用户上移则放行其位置） */
  useEffect(() => {
    if (prevGenerating.current && !isGenerating && isFollowingBottom.current) {
      const timer = setTimeout(() => {
        if (
          !segmentSwapHoldRef.current &&
          !anchorRestoreActiveRef.current &&
          isFollowingBottom.current
        ) {
          pinToBottom()
        }
      }, 300)
      return () => clearTimeout(timer)
    }
    prevGenerating.current = isGenerating
  }, [isGenerating, pinToBottom])

  /**
   * 组件卸载时缓存当前滚动定位点（供下次切换回来恢复）
   *
   * 读 onScroll/采样实时记录的 ref（距底距离 + 翻历史锚点行），而不读 DOM：
   * 切 Tab 卸载时 React 先清空消息 DOM（scrollHeight/scrollTop 归 0），再跑 cleanup，
   * 此时读 DOM 全是垃圾值 0（拆除期幽灵 scroll 事件已由 onScroll 的 0 尺寸防御
   * 拦截，不会污染 ref）。距底距离的初值 0 恰是合法语义"钉在底部"，因此消息
   * 未加载就切走（从未定位/滚动）落缓存 0，重进恢复即钉底——不会像绝对 scrollTop
   * 那样把 0 当成"顶部"恢复。
   * effect 运行时（commit 后）闭包捕获 DOM 引用；不在 cleanup 里直接读 ref，
   * 因为卸载时 React 先 detach ref（置 null）再跑 passive effect cleanup。
   */
  useEffect(() => {
    return () => {
      if (tabId) {
        scrollRestoreCache.set(tabId, {
          anchorId: lastScrollAnchorRef.current?.id ?? null,
          anchorOffset: lastScrollAnchorRef.current?.offset ?? 0,
          distanceFromBottom: lastDistanceFromBottomRef.current,
        })
      }
    }
  }, [tabId])

  /** 切换会话时重置初始滚动标记 */
  useEffect(() => {
    if (messages.length === 0) {
      initialScrollDone.current = false
    }
  }, [tabId])

  /** 空状态 / 加载中 / 加载失败态渲染 */
  if (messages.length === 0) {
    // X1-F1：当前管道加载失败时渲染错误态而非欢迎空态——空态会让用户误以为
    // 消息丢失；错误提示 + 重新加载按钮给用户可见反馈与手动自愈入口。
    if (activePipelineLoadError) {
      return (
        <div
          className={`flex flex-1 items-center justify-center ${className}`}
          data-testid="message-list-load-error"
        >
          <div className="text-muted-foreground text-center">
            <AlertCircle className="mx-auto mb-3 h-12 w-12 opacity-50" />
            <p>消息加载失败</p>
            <p className="mt-1 text-xs">网络异常或服务暂不可用，消息并未丢失</p>
            <button
              type="button"
              onClick={handleRetryLoad}
              data-testid="message-list-retry-load"
              className="mt-4 inline-flex items-center gap-1.5 rounded-md border px-4 py-1.5 text-sm transition-colors hover:bg-muted"
            >
              <RefreshCw className="h-icon-sm w-icon-sm" />
              重新加载
            </button>
          </div>
        </div>
      )
    }
    // 首屏消息 GET 进行中（会话切换/子 Tab 首载）：骨架占位而非空态文案——
    // 空 ≠ 加载（OBS-R258-1 四态约定），「开始新的对话」只在确认无消息后出现
    if (activePipelineInitialLoading || tabMessagesLoading) {
      return (
        <div className={cn('flex flex-1 flex-col', className)} data-testid="message-list-initial-loading">
          <div className="mx-auto w-full max-w-[var(--chat-content-max-w)] space-y-4 px-4 py-6">
            {['w-[80%]', 'w-[60%]', 'w-[72%]', 'w-[48%]'].map((widthClass, i) => (
              <div
                key={i}
                className={cn(
                  'bg-muted animate-pulse rounded-lg px-4 py-3',
                  widthClass,
                  i % 2 === 1 && 'ml-auto',
                )}
              />
            ))}
          </div>
        </div>
      )
    }
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
    <div className={cn('relative flex min-h-0 flex-col', className)}>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="min-h-0 w-full flex-1 overflow-y-auto"
        style={{ overflowAnchor: 'auto' }}
        data-testid="message-list"
      >
      {/* 对话列居中上限（--chat-content-max-w）：宽视口下行长收敛；窄于上限
          （移动端/分屏）时 w-full 铺满零变化。与输入区同一变量保持同轴。 */}
      <div ref={contentRef} className="mx-auto w-full max-w-[var(--chat-content-max-w)]">
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

        {/* 消息列表（压缩块按段归组为小条+原文还原；其余渲染合并后的气泡，
            连续 assistant 合为一条） */}
        {renderedRows}

        <div className="h-icon-md" />
      </div>
      </div>

      {/* 「回到底部」浮钮（成熟配方B，全端通用）：跟随暂停且已离开底部阈值区
          时出现，点按平滑回底并恢复跟随。纯图标圆形小浮钮（标准聊天位），悬浮
          于滚动容器右下角不参与文档流；无障碍名 aria-label 承载。 */}
      {showScrollToBottom && (
        <button
          type="button"
          onClick={handleScrollToBottom}
          aria-label="回到底部"
          title="回到底部"
          className="bg-background/95 text-muted-foreground hover:bg-muted hover:text-foreground absolute bottom-3 right-3 z-10 flex h-9 w-9 items-center justify-center rounded-full border shadow-md backdrop-blur transition-colors"
          data-testid="message-list-scroll-to-bottom"
        >
          <ChevronDown className="h-icon-md w-icon-md" />
        </button>
      )}
    </div>
  )
}
