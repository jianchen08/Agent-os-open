/**
 * ‹i/n› 多代切换器（消息段模型：分支版本切换）
 *
 * 独立于 MessageActions 的操作组渲染——切换器是到达历史代版本的**唯一入口**，
 * 藏在 hover 才显形的操作组里等于「没有入口」（用户实证：重试多次仍找不到
 * 左右切换按钮）。故本组件常驻显示，MessageItem 在元信息行单独挂载。
 *
 * 位置列表 = [seg1..segn]（按 created_at 升序，代号 = 序数）+ 启用序列（最新代）。
 * viewOrdinal 0 = 正看启用序列（标签「当前」），k = 正看第 k 段（乐观换装态）。
 */

import { toast } from 'sonner'
import { useShallow } from 'zustand/react/shallow'
import { getMessageSegmentDetail } from '@/services/api/messageSegments'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import type { SegmentMeta } from '@/services/api/messageSegments'
import type { Message } from '@/types/models'

/** 空段列表复用实例（useShallow 选择器基线） */
const EMPTY_SEGMENTS: SegmentMeta[] = []

export interface SegmentSwitcherProps {
  /** 消息对象（换装落地的目标会话坐标来源） */
  message: Message
  /** 会话 ID */
  sessionId: string
  /** 是否禁用（生成态：服务端约束 §7.3 运行中激活必拒） */
  disabled?: boolean
  /** ‹i/n› 多代切换器锚点 base_seq（消息段模型；null/缺省不显示切换器，
   *  由 MessageList 按「轮末条 assistant + 锚点多段」标定） */
  segmentSwitcherBaseSeq?: number | null
}

/**
 * ‹i/n› 多代切换器组件
 */
export const SegmentSwitcher = ({
  message,
  sessionId,
  disabled = false,
  segmentSwitcherBaseSeq,
}: SegmentSwitcherProps) => {
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)
  const segmentView = usePipelineMessageStore((s) =>
    activePipelineId ? s.segmentViewByPipeline[activePipelineId] : undefined,
  )
  const segmentsAtAnchor = usePipelineMessageStore(
    useShallow((s) => {
      if (!activePipelineId || segmentSwitcherBaseSeq == null) return EMPTY_SEGMENTS
      return (s.spanIndex[activePipelineId] || [])
        .filter((seg) => seg.base_seq === segmentSwitcherBaseSeq)
        .sort((a, b) => a.created_at.localeCompare(b.created_at))
    }),
  )
  const segmentCount = segmentsAtAnchor.length
  const matchedIdx = segmentView
    ? segmentsAtAnchor.findIndex((seg) => seg.id === segmentView.segmentId)
    : -1
  const viewOrdinal =
    segmentView && segmentView.baseSeq === segmentSwitcherBaseSeq && matchedIdx >= 0
      ? matchedIdx + 1
      : 0

  /**
   * 切换到第 targetOrdinal 段：GET 段详情 → 乐观换装（applySegmentView）→
   * 发 segment_activate，启用序列由 segment_activated ack 对账纠偏。
   * 运行中预检（服务端约束 §7.3：run 活跃时激活必拒「任务运行中」）——
   * 直接 toast，不乐观换装、不出站（不改启用状态）。
   */
  const handleSegmentSwitch = (targetOrdinal: number) => {
    const ps = usePipelineMessageStore.getState()
    const pipelineId = ps.activePipelineId
    if (!pipelineId || !sessionId) return
    if (disabled || ps.isStreaming(pipelineId)) {
      toast.error('任务运行中，无法切换对话版本')
      return
    }
    const target = segmentsAtAnchor[targetOrdinal - 1]
    if (!target) return
    void getMessageSegmentDetail(target.id)
      .then((detail) => {
        // 取详情期间管道可能已开始运行：同样按运行中拒绝，保持乐观态不落地
        if (usePipelineMessageStore.getState().isStreaming(pipelineId)) {
          toast.error('任务运行中，无法切换对话版本')
          return
        }
        usePipelineMessageStore
          .getState()
          .applySegmentView(pipelineId, detail, message.sessionId || sessionId)
        globalWS.sendSegmentActivate(sessionId, { pipelineId, segmentId: target.id })
      })
      .catch(() => {
        toast.error('获取对话版本失败，请稍后重试')
      })
  }

  // 锚点多段时渲染：‹ 上一代 · 当前|k/n · 下一代 ›
  if (segmentSwitcherBaseSeq == null || segmentCount <= 1) return null
  return (
    <span
      className="flex items-center gap-0.5 rounded-md bg-muted/60 px-1 py-0.5 text-xs"
      data-testid="segment-switcher"
    >
      <button
        type="button"
        className="px-0.5 font-medium hover:opacity-80 disabled:opacity-30"
        data-testid="segment-prev"
        aria-label="上一代"
        title="上一代"
        disabled={viewOrdinal === 1}
        onClick={() => handleSegmentSwitch(viewOrdinal === 0 ? segmentCount : viewOrdinal - 1)}
      >
        ‹
      </button>
      <span className="text-muted-foreground px-0.5" data-testid="segment-position">
        {viewOrdinal > 0 ? `${viewOrdinal}/${segmentCount}` : `当前/${segmentCount}`}
      </span>
      <button
        type="button"
        className="px-0.5 font-medium hover:opacity-80 disabled:opacity-30"
        data-testid="segment-next"
        aria-label="下一代"
        title="下一代"
        disabled={viewOrdinal === 0 || viewOrdinal >= segmentCount}
        onClick={() => handleSegmentSwitch(viewOrdinal + 1)}
      >
        ›
      </button>
    </span>
  )
}
