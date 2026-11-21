/**
 * 上下文使用进度指示器（圈型进度 + 用量明细浮窗）
 *
 * 主条：圆环（模型名前的头部图标槽位）| 模型名 | 已用/上限（紧凑数字）。
 * 窗口已知（maxTokens > 0）：圆环带 role=progressbar 与 aria 值，按真实比例
 * 渲染进度弧，色调语义 >=90% error 红 / >=70% warning 黄 / 其余 success 绿。
 * 窗口未知（maxTokens <= 0）：圆环仍在（无进度语义的图标槽位，进度弧为空、
 * muted 色），数字不显示——不展示假进度。
 *
 * 浮窗：鼠标悬停或点击弹出，展示上下文使用量、本轮 token 明细（输出/总计/
 * 缓存命中）与该管道的累计 token 明细；悬停移入浮窗不关闭，再次点击关闭。
 * 定位：浮窗只落在消息流区（MessageList 的 data-chat-flow-region 区域）内——
 * 以圆环为锚点向上弹出，再按流区四边夹紧（水平跟随圆环，垂直贴流区底部；
 * 空间不足时收缩并在内部滚动），流区尺寸变化时重算；找不到消息流区（侧栏等
 * 非聊天场景）回退为锚点上方左对齐的旧定位。
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'
import { createPortal } from 'react-dom'
import { AlertCircle } from '@/assets/icons'
import { cn } from '@/lib/utils'
import { formatNumber } from '@/utils/format'
import type { CumulativeUsage } from '@/stores/contextUsageStore'

export interface ContextUsageIndicatorProps {
  /** 模型名；空则显示「模型无效」 */
  modelName?: string | null
  /** 已使用上下文 token（通常为 promptTokens） */
  currentTokenUsage?: number
  /** 上下文窗口上限；<=0 时不显示进度 */
  maxTokens?: number
  /** 本轮总 token（输入+输出，浮窗明细） */
  totalTokens?: number
  /** 本轮输出 token（浮窗明细） */
  completionTokens?: number
  /** 本轮缓存命中 token（浮窗明细） */
  cachedTokens?: number
  /** 本轮缓存命中率 0-1（浮窗明细） */
  hitRatio?: number
  /** 该管道累计消耗（浮窗明细；无累计数据不显示累计段） */
  cumulative?: CumulativeUsage
  /** 紧凑模式（小尺寸，侧栏/旧 StatusBar 场景） */
  compact?: boolean
  /** 纯圈模式（chat-input 拆分裁定 20261023）：只渲染圆环本身，模型名与行内
   *  已用/上限数字一律不渲染，上下文信息只在悬停/点击浮窗呈现；缺省 false =
   *  沿用旧形态（圈+名+数字，侧栏等场景） */
  ringOnly?: boolean
  /** 压缩终态（管道 state `context.compression`）：failed 时常驻可见地标注
   *  「压缩失败」——压缩失败 = 上下文持续膨胀的静默劣化，只发一次性 toast
   *  等于没有提示（用户裁定 2026-10-28） */
  compressionStatus?: { status: string; reason?: string }
  className?: string
}

/** 圆环尺寸（compact 12 / 常规 16） */
const RING_SIZE = 16
const RING_SIZE_COMPACT = 12

/** 浮窗与消息流区边缘的最小间距 */
const POPOVER_MARGIN = 8
/** 浮窗与触发圆环之间的默认间距（未受限时由 pb-2 hover 桥承载） */
const POPOVER_GAP = 8
/** 浮窗首选宽度（w-64 = 16rem；根字号被主题放大时以实测宽度为准） */
const POPOVER_WIDTH = 256
/** 受限定位下指针穿越输入区空隙的移入宽限（毫秒） */
const POPOVER_CLOSE_DELAY_MS = 220

/** 受限定位结果（视口坐标：消息流区内夹紧后的浮窗落点） */
interface PopoverLayout {
  left: number
  top: number
  width: number
  maxHeight: number
}

/**
 * 消息流区：聊天容器内带 data-chat-flow-region 的区域（MessageList 各渲染态
 * 的根节点）。浮窗只允许落在该区域内；找不到（侧栏等非聊天场景）返回 null，
 * 调用方回退旧定位。
 */
function findChatFlowRegion(anchor: HTMLElement): HTMLElement | null {
  const container = anchor.closest('[data-testid="chat-container"]')
  return container?.querySelector<HTMLElement>('[data-chat-flow-region]') ?? null
}

/** token 紧凑格式（主条用）：1000→1.0k，128000→128k，2500000→2.5M */
export function formatCompactTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`
  if (n >= 100_000) return `${Math.round(n / 1_000)}k`
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`
  return String(n)
}

/** 浮窗明细行（有值才渲染） */
function DetailRow({ label, value, ratio }: { label: string; value?: number; ratio?: number }) {
  if (!value || value <= 0) return null
  return (
    <div className="flex items-center justify-between gap-4">
      <span className="text-muted-foreground">{label}</span>
      <span className="font-medium tabular-nums">
        {formatNumber(value)}
        {typeof ratio === 'number' && ratio > 0 ? ` (${Math.round(ratio * 100)}%)` : ''}
      </span>
    </div>
  )
}

/**
 * 圆环（SVG 圆环）：模型名前的头部图标槽位。窗口已知时带 role=progressbar
 * 供无障碍，颜色语义随使用率（error/warning/success）；窗口未知时仅为图标
 * （无进度语义，进度弧为空、muted 色）。
 */
function UsageRing({
  ratio,
  size,
  tone,
  withProgress,
}: {
  ratio: number
  size: number
  tone: string
  /** 上下文窗口已知才带进度条语义（role/aria 值）；未知时仅为图标槽位 */
  withProgress: boolean
}) {
  const stroke = 2
  const r = (size - stroke) / 2
  const c = 2 * Math.PI * r
  const clamped = Math.max(0, Math.min(1, ratio))
  const progressAria = withProgress
    ? {
        role: 'progressbar' as const,
        'aria-valuenow': Math.round(clamped * 100),
        'aria-valuemin': 0,
        'aria-valuemax': 100,
        'aria-label': '上下文用量',
      }
    : {}

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      {...progressAria}
      className="shrink-0"
      data-testid="context-usage-ring"
    >
      {/* 轨道 */}
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke="currentColor"
        className="text-primary/15"
        strokeWidth={stroke}
      />
      {/* 进度（从 12 点方向顺时针） */}
      <circle
        cx={size / 2}
        cy={size / 2}
        r={r}
        fill="none"
        stroke="currentColor"
        className={cn(tone)}
        strokeWidth={stroke}
        strokeLinecap="round"
        strokeDasharray={c}
        strokeDashoffset={c * (1 - clamped)}
        transform={`rotate(-90 ${size / 2} ${size / 2})`}
        style={{ transition: 'stroke-dashoffset 0.3s ease' }}
      />
    </svg>
  )
}

/**
 * 用量明细浮窗：上下文使用量 + 本轮明细 + 管道累计明细（段内无数据的行不渲染）。
 * 定位：layout 非空 = 消息流区内受限定位（fixed 视口坐标，调用方已夹紧）；
 * 为空 = 锚点上方左对齐的旧定位（非聊天场景/布局不可测量）。
 */
function UsagePopover({
  modelName,
  currentTokenUsage,
  maxTokens,
  totalTokens,
  completionTokens,
  cachedTokens,
  hitRatio,
  cumulative,
  compressionStatus,
  layout,
  panelRef,
  onMouseEnter,
  onMouseLeave,
}: {
  modelName: string
  currentTokenUsage: number
  maxTokens: number
  totalTokens: number
  completionTokens: number
  cachedTokens: number
  hitRatio: number
  cumulative?: CumulativeUsage
  compressionStatus?: { status: string; reason?: string }
  layout: PopoverLayout | null
  panelRef: RefObject<HTMLDivElement | null>
  onMouseEnter: () => void
  onMouseLeave: () => void
}) {
  const ratio = maxTokens > 0 ? Math.min(1, currentTokenUsage / maxTokens) : 0
  return (
    // 外层承载定位与 hover 桥：未受限时 pb-2 把视觉间隙纳入浮窗 hover 区
    // （鼠标从主条移入浮窗不穿缝隙、不闪关）；受限时由调用方按消息流区夹紧
    <div
      className={cn('z-[100]', layout ? 'fixed' : 'absolute bottom-full left-0 pb-2')}
      /* eslint-disable-next-line no-restricted-syntax -- 受限定位落点逐次测量，Tailwind 静态类无法承载 */
      style={layout ? { left: layout.left, top: layout.top } : undefined}
      onMouseEnter={onMouseEnter}
      onMouseLeave={onMouseLeave}
      data-testid="context-usage-popover"
    >
      <div
        ref={panelRef}
        className={cn(
          'border-border bg-[var(--ds-bg-panel,hsl(var(--card)))] rounded-lg border p-3 shadow-xl',
          layout ? 'overscroll-contain overflow-y-auto' : 'w-64',
        )}
        /* eslint-disable-next-line no-restricted-syntax -- 受限定位宽高由消息流区实测决定，Tailwind 静态类无法承载 */
        style={layout ? { width: layout.width, maxHeight: layout.maxHeight } : undefined}
        data-testid="context-usage-popover-panel"
      >
      {/* 上下文使用量 */}
      <div className="text-muted-foreground mb-1.5 text-[10px] font-medium tracking-wide">
        上下文 · {modelName}
      </div>
      {maxTokens > 0 && (
        <div className="bg-primary/15 mb-1 h-1.5 overflow-hidden rounded-full">
          <div
            className="bg-primary h-full rounded-full"
            style={{ width: `${ratio * 100}%` }}
            data-testid="context-usage-popover-bar"
          />
        </div>
      )}
      <div className="flex items-center justify-between gap-4 text-xs">
        <span className="text-muted-foreground">{maxTokens > 0 ? '已用 / 上限' : '已用'}</span>
        <span className="font-medium tabular-nums">
          {formatNumber(currentTokenUsage)}
          {maxTokens > 0 ? ` / ${formatNumber(maxTokens)}（${Math.round(ratio * 100)}%）` : ''}
        </span>
      </div>

      {/* 本轮 token 明细 */}
      {(totalTokens > 0 || completionTokens > 0 || cachedTokens > 0) && (
        <div className="border-border mt-2 border-t pt-2 text-xs">
          <div className="text-muted-foreground mb-1 text-[10px] font-medium tracking-wide">
            本轮
          </div>
          <div className="space-y-0.5">
            <DetailRow label="输入" value={currentTokenUsage} />
            <DetailRow label="输出" value={completionTokens} />
            <DetailRow label="总计" value={totalTokens} />
            <DetailRow label="缓存命中" value={cachedTokens} ratio={hitRatio} />
          </div>
        </div>
      )}

      {/* 上下文压缩终态（失败 = 上下文持续膨胀，必须可见） */}
      {compressionStatus && (
        <div className="border-border mt-2 border-t pt-2 text-xs">
          <div className="text-muted-foreground mb-1 text-[10px] font-medium tracking-wide">
            上下文压缩
          </div>
          <div
            className={cn(
              'leading-snug',
              compressionStatus.status === 'failed' ? 'text-status-error' : 'text-status-success',
            )}
            data-testid="context-compression-status"
          >
            {compressionStatus.status === 'failed'
              ? `失败：${compressionStatus.reason || '未产生有效收缩，上下文将持续膨胀'}`
              : '正常（最近一次已生效）'}
          </div>
        </div>
      )}

      {/* 该管道累计 token 明细 */}
      {cumulative && cumulative.total_tokens > 0 && (
        <div className="border-border mt-2 border-t pt-2 text-xs">
          <div className="text-muted-foreground mb-1 text-[10px] font-medium tracking-wide">
            本管道累计
          </div>
          <div className="space-y-0.5">
            <DetailRow label="输入" value={cumulative.total_input} />
            <DetailRow label="输出" value={cumulative.total_output} />
            <DetailRow label="总计" value={cumulative.total_tokens} />
            <DetailRow
              label="缓存命中"
              value={cumulative.total_cached}
              ratio={cumulative.cache_hit_ratio}
            />
          </div>
        </div>
      )}
      </div>
    </div>
  )
}

/**
 * 圆环 + 模型名 + 用量明细浮窗（输入框工具栏槽位默认件）
 */
export function ContextUsageIndicator({
  modelName,
  currentTokenUsage = 0,
  maxTokens = 0,
  totalTokens = 0,
  completionTokens = 0,
  cachedTokens = 0,
  hitRatio = 0,
  cumulative,
  compact = false,
  ringOnly = false,
  compressionStatus,
  className,
}: ContextUsageIndicatorProps) {
  const [popoverOpen, setPopoverOpen] = useState(false)
  const [popoverLayout, setPopoverLayout] = useState<PopoverLayout | null>(null)
  /** 触发圆环（消息流区定位的锚点与流区查找起点） */
  const anchorRef = useRef<HTMLDivElement>(null)
  const panelRef = useRef<HTMLDivElement>(null)
  /** 未受限形态下实测的自然宽度（受限后 rect 是夹紧值，不能反推自然宽） */
  const naturalWidthRef = useRef(0)
  const closeTimerRef = useRef<number | null>(null)

  const cancelScheduledClose = useCallback(() => {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current)
      closeTimerRef.current = null
    }
  }, [])

  /**
   * 测量触发圆环与消息流区，计算浮窗在流区内的落点：水平跟随圆环并夹在
   * 左右边界内，垂直贴流区底部；流区放不下时收缩尺寸（面板内部滚动）。
   * 布局不可测量（未上屏/jsdom）或无消息流区时置空，回退旧定位。
   */
  const measurePopoverLayout = useCallback(() => {
    const anchor = anchorRef.current
    const panel = panelRef.current
    if (!anchor || !panel) return
    const region = findChatFlowRegion(anchor)
    if (!region) {
      setPopoverLayout(null)
      return
    }
    const bounds = region.getBoundingClientRect()
    if (bounds.width <= 0 || bounds.height <= 0) {
      setPopoverLayout(null)
      return
    }
    const anchorRect = anchor.getBoundingClientRect()
    const panelRect = panel.getBoundingClientRect()
    if (!popoverLayout && panelRect.width > 0) naturalWidthRef.current = panelRect.width
    const naturalWidth = naturalWidthRef.current || POPOVER_WIDTH
    // scrollHeight = 内容自然高（不受当前 maxHeight 夹紧影响），流区变大后能恢复
    const naturalHeight = panel.scrollHeight || panelRect.height

    const marginLeft = bounds.left + POPOVER_MARGIN
    const marginRight = bounds.right - POPOVER_MARGIN
    const marginTop = bounds.top + POPOVER_MARGIN
    const marginBottom = bounds.bottom - POPOVER_MARGIN

    const width = Math.min(naturalWidth, Math.max(0, marginRight - marginLeft))
    const maxHeight = Math.min(naturalHeight, Math.max(0, marginBottom - marginTop))

    let left = anchorRect.left
    if (left + width > marginRight) left = marginRight - width
    if (left < marginLeft) left = marginLeft

    const bottom = Math.min(anchorRect.top - POPOVER_GAP, marginBottom)
    let top = bottom - maxHeight
    if (top < marginTop) top = marginTop

    const next: PopoverLayout = {
      left: Math.round(left),
      top: Math.round(top),
      width: Math.round(width),
      maxHeight: Math.round(maxHeight),
    }
    setPopoverLayout((prev) =>
      prev &&
      prev.left === next.left &&
      prev.top === next.top &&
      prev.width === next.width &&
      prev.maxHeight === next.maxHeight
        ? prev
        : next,
    )
  }, [popoverLayout])

  /** 打开期间每次渲染后重新测量：内容增减/主题字号变化都重新夹紧 */
  useLayoutEffect(() => {
    if (!popoverOpen) return
    measurePopoverLayout()
  })

  /** 视口/消息流区/锚点尺寸变化时重算（窗口缩放、输入区展开、侧栏折叠等） */
  useEffect(() => {
    if (!popoverOpen) return
    const anchor = anchorRef.current
    const region = anchor ? findChatFlowRegion(anchor) : null
    const handleViewportChange = () => measurePopoverLayout()
    window.addEventListener('resize', handleViewportChange)
    let observer: ResizeObserver | undefined
    if (typeof ResizeObserver !== 'undefined') {
      observer = new ResizeObserver(handleViewportChange)
      if (region) observer.observe(region)
      if (anchor) observer.observe(anchor)
    }
    return () => {
      window.removeEventListener('resize', handleViewportChange)
      observer?.disconnect()
    }
  }, [popoverOpen, measurePopoverLayout])

  /** 卸载清理关闭定时器 */
  useEffect(() => cancelScheduledClose, [cancelScheduledClose])

  const openPopover = useCallback(() => {
    cancelScheduledClose()
    setPopoverOpen(true)
  }, [cancelScheduledClose])

  const schedulePopoverClose = useCallback(() => {
    cancelScheduledClose()
    if (!popoverLayout) {
      setPopoverOpen(false)
      return
    }
    // 受限定位时浮窗底边被夹在消息流区内，与触发圆环之间隔着输入区：
    // 指针需穿越该空隙，延迟关闭留出移入浮窗的时间
    closeTimerRef.current = window.setTimeout(() => {
      closeTimerRef.current = null
      setPopoverOpen(false)
    }, POPOVER_CLOSE_DELAY_MS)
  }, [cancelScheduledClose, popoverLayout])

  if (!modelName || modelName === 'unknown') {
    return (
      <div
        className={cn(
          'items-center gap-1.5 rounded-lg border border-muted/20 text-muted-foreground',
          compact
            ? 'flex h-[18px] px-1.5 text-[10px]'
            : 'flex h-8 px-2 text-xs sm:px-3',
          className,
        )}
        data-testid="context-usage-invalid"
      >
        <AlertCircle className={compact ? 'h-icon-xs w-icon-xs' : 'h-icon-sm w-icon-sm'} />
        <span>模型无效</span>
      </div>
    )
  }

  const hasWindow = maxTokens > 0
  const compressionFailed = compressionStatus?.status === 'failed'
  const ratio = hasWindow ? currentTokenUsage / maxTokens : 0
  const tone = !hasWindow
    ? 'text-muted-foreground'
    : ratio >= 0.9
      ? 'text-status-error'
      : ratio >= 0.7
        ? 'text-status-warning'
        : 'text-status-success'

  return (
    <div className={cn('relative', className)}>
      <div
        ref={anchorRef}
        className={cn(
          'bg-primary/10 border-primary/20 min-w-0 cursor-pointer items-center gap-2 rounded-lg border',
          compact
            ? 'flex h-[18px] gap-1.5 px-1.5 text-[10px]'
            : 'flex h-8 gap-1.5 px-2 text-xs sm:gap-2 sm:px-3',
        )}
        data-testid="context-usage-indicator"
        aria-expanded={popoverOpen}
        onClick={() => {
          cancelScheduledClose()
          setPopoverOpen((v) => !v)
        }}
        onMouseEnter={openPopover}
        onMouseLeave={schedulePopoverClose}
      >
        <UsageRing
          ratio={ratio}
          size={compact ? RING_SIZE_COMPACT : RING_SIZE}
          tone={tone}
          withProgress={hasWindow}
        />
        {!ringOnly && (
          <span className="text-primary max-w-[72px] truncate font-semibold sm:max-w-[120px]">{modelName}</span>
        )}
        {!ringOnly && hasWindow && (
          <span className="text-primary/80 tabular-nums whitespace-nowrap">
            {formatCompactTokens(currentTokenUsage)} / {formatCompactTokens(maxTokens)}
          </span>
        )}
        {compressionFailed &&
          (ringOnly ? (
            // 纯圈模式无文案位：一个红点作常驻信号（详情在悬停浮窗）
            <span
              className="bg-status-error h-1.5 w-1.5 shrink-0 rounded-full"
              data-testid="context-compression-failed-dot"
              aria-label="上下文压缩失败"
            />
          ) : (
            <span
              className="text-status-error border-status-error/40 shrink-0 rounded border px-1 text-[10px] whitespace-nowrap"
              data-testid="context-compression-failed"
            >
              压缩失败
            </span>
          ))}
      </div>
      {popoverOpen &&
        (() => {
          const popover = (
            <UsagePopover
              modelName={modelName}
              currentTokenUsage={currentTokenUsage}
              maxTokens={maxTokens}
              totalTokens={totalTokens}
              completionTokens={completionTokens}
              cachedTokens={cachedTokens}
              hitRatio={hitRatio}
              cumulative={cumulative}
              compressionStatus={compressionStatus}
              layout={popoverLayout}
              panelRef={panelRef}
              onMouseEnter={openPopover}
              onMouseLeave={schedulePopoverClose}
            />
          )
          // 受限定位 portal 到 body：不被聊天容器/输入区的 overflow 裁剪；
          // 未受限（非聊天场景）保持原地的绝对定位
          return popoverLayout ? createPortal(popover, document.body) : popover
        })()}
    </div>
  )
}
