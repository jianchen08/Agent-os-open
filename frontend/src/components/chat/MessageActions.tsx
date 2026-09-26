/**
 * 消息操作按钮组件
 *
 * 提供消息的复制、编辑、回退（二次确认）与「重新生成」操作。
 * 所有操作入口均以回调形式暴露，调用方决定是否接入（不传回调即不显示按钮）。
 */

import { useState, type FC } from 'react'
import { toast } from 'sonner'
import { useShallow } from 'zustand/react/shallow'
import { Copy, Pencil, RotateCcw, RefreshCw } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { getMessageSegmentDetail } from '@/services/api/messageSegments'
import { globalWS } from '@/services/websocket/GlobalWebSocket'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import type { SegmentMeta } from '@/services/api/messageSegments'
import type { Message } from '@/types/models'

/**
 * 消息操作组件属性
 */
export interface MessageActionsProps {
  /** 消息对象 */
  message: Message
  /** 会话 ID */
  sessionId: string
  /** 是否为用户消息 */
  isUserMessage?: boolean
  /** 是否禁用操作 */
  disabled?: boolean
  /** 是否为最后一条消息（控制重试/编辑回复按钮显示） */
  isLastMessage?: boolean
  /** 消息复制回调 */
  onCopy?: () => void
  /** 消息编辑回调（打开内联编辑器，保存路径由 MessageItem 接管） */
  onEdit?: () => void
  /** 消息内容更新回调（用于版本切换） */
  onContentUpdate?: (content: string) => void
  /** 重新生成回调（最后一条 assistant 消息，含失败/中断态） */
  onRegenerate?: () => void
  /** 回退回调（user 消息二次确认后触发，参数为目标 user 消息 ID） */
  onRollbackTo?: (userMessageId: string) => void
  /** ‹i/n› 多代切换器锚点 base_seq（消息段模型；null/缺省不显示切换器，
   *  由 MessageList 按「轮末条 assistant + 锚点多段」标定） */
  segmentSwitcherBaseSeq?: number | null
}

/** 判定消息是否处于失败/中断态（空内容时仍需保留可重试入口） */
function isFailedOrInterrupted(message: Message): boolean {
  return (
    message.status === 'error' ||
    message.status === 'failed' ||
    message.status === 'interrupted'
  )
}

/** 空段列表复用实例（useShallow 选择器基线） */
const EMPTY_SEGMENTS: SegmentMeta[] = []

/**
 * 消息操作按钮组件
 */
export const MessageActions: FC<MessageActionsProps> = ({
  message,
  sessionId,
  disabled = false,
  onCopy,
  onEdit,
  onRollbackTo,
  onRegenerate,
  segmentSwitcherBaseSeq,
}) => {
  /** 回退二次确认（内联展开确认条） */
  const [confirmingRollback, setConfirmingRollback] = useState(false)
  /** 是否为用户消息（决定回退/编辑入口归属） */
  const isUser = message.role === 'user'
  /** 是否为最后一条 assistant 消息（重新生成入口：含失败/中断态） */
  const isAssistant = message.role === 'assistant'
  const showRegenerate =
    isAssistant && !!onRegenerate && (isFailedOrInterrupted(message) || message.status === 'completed')

  // ── ‹i/n› 多代切换器（消息段模型）─────────────────────────────────────
  // 位置列表 = [seg1..segn]（按 created_at 升序，代号 = 序数）+ 启用序列（最新代）。
  // viewOrdinal 0 = 正看启用序列（标签「当前」），k = 正看第 k 段（乐观换装态）。
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
  const viewOrdinal = segmentView && segmentView.baseSeq === segmentSwitcherBaseSeq && matchedIdx >= 0
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

  /** 切换器（锚点多段时渲染）：‹ 上一代 · 当前|k/n · 下一代 › */
  const renderSegmentSwitcher = () => {
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

  /**
   * 处理复制操作
   */
  const handleCopy = () => {
    if (onCopy) {
      onCopy()
    } else {
      navigator.clipboard.writeText(message.content)
      toast.success('已复制到剪贴板')
    }
  }

  /** 确认回退：触发回调并收起确认条 */
  const handleConfirmRollback = () => {
    setConfirmingRollback(false)
    if (onRollbackTo) {
      onRollbackTo(message.id)
    }
  }

  /** 二次确认条（内联展开，确认前不产生任何副作用） */
  const renderRollbackConfirm = () => {
    if (!confirmingRollback) return null
    return (
      <span
        className="flex items-center gap-1 rounded-md bg-[color-mix(in_srgb,var(--status-warning)_15%,transparent)] px-1.5 py-0.5 text-xs text-[var(--status-warning)]"
        data-testid="rollback-confirm"
      >
        <span>确认回退？</span>
        <button
          type="button"
          className="font-medium underline hover:opacity-80"
          onClick={handleConfirmRollback}
          disabled={disabled}
        >
          确认
        </button>
        <button
          type="button"
          className="opacity-70 hover:opacity-100"
          onClick={() => setConfirmingRollback(false)}
          disabled={disabled}
        >
          取消
        </button>
      </span>
    )
  }

  return (
    <div className="flex items-center gap-0.5 opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity">
      <Button
        variant="ghost"
        size="sm"
        className="h-6 w-6 p-0"
        onClick={handleCopy}
        disabled={disabled}
        title="复制"
      >
        <Copy className="h-icon-xs w-icon-xs" />
      </Button>
      {/* 用户消息操作：编辑（打开内联编辑器）+ 回退（二次确认） */}
      {isUser && onEdit && (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 w-6 p-0"
          onClick={onEdit}
          disabled={disabled}
          title="编辑并重新发送"
        >
          <Pencil className="h-icon-xs w-icon-xs" />
        </Button>
      )}
      {isUser && onRollbackTo && (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 w-6 p-0"
          onClick={() => setConfirmingRollback(true)}
          disabled={disabled}
          title="回退到这条消息"
        >
          <RotateCcw className="h-icon-xs w-icon-xs" />
        </Button>
      )}
      {renderRollbackConfirm()}
      {/* ‹i/n› 多代切换器（锚点多段时渲染） */}
      {renderSegmentSwitcher()}
      {/* 重新生成：最后一条 assistant 消息（含失败/中断态） */}
      {showRegenerate && (
        <Button
          variant="ghost"
          size="sm"
          className="h-6 w-6 p-0"
          onClick={onRegenerate}
          disabled={disabled}
          title="重新生成"
          data-testid="regenerate-button"
        >
          <RefreshCw className="h-icon-xs w-icon-xs" />
        </Button>
      )}
    </div>
  )
}
