/**
 * 消息底部活动指示行（单一状态指示点）
 *
 * 2026-11-19 用户历次裁定合并后的最终契约：
 * - **每张活动卡片只有一处状态指示**：就是本行（底部、带转圈、后跟耗时）。
 *   卡片头部/思考正文右侧**不得**再出现任何状态文字标签。
 * - **全站活动态文案只有一个词「执行中」**：等待、思考内容输出中、正答输出中、
 *   工具运行中、轮次间隙——圈旁一律「执行中」，不出现「思考中 / 输出中 /
 *   生成中 / 处理中」等变体。
 * - 唯一例外「压缩中」：上下文压缩是分钟级同步调用，与「等模型」语义不同，
 *   保留 2026-10-28 用户裁定的可辨别性。
 * - 审批等待态不显示（等待用户归交互卡，此处显示是误报）。
 * - 状态切换由流结束事件驱动（终止事件即清流式态），不等轮询节奏。
 */

import { StreamActivityIndicator } from './StreamActivityIndicator'
import type { MessagePart } from '@/types/messageParts'
import type { ReactNode } from 'react'

export function ToolTailActivity({
  streaming,
  pendingInteraction,
  compressing,
  streamAnchorMs,
}: {
  streaming: boolean
  /** 消息 parts（保留入参以兼容既有调用签名；文案已不按 parts 阶段分叉） */
  parts?: MessagePart[]
  pendingInteraction: boolean
  /** 上下文压缩中：指示文案切「压缩中」（用户裁定 2026-10-28） */
  compressing?: boolean
  /** 实时总耗时锚点（触发消息发出时刻 ms）；缺席不显示耗时 */
  streamAnchorMs?: number | null
}): ReactNode {
  if (!streaming || pendingInteraction) return null

  return (
    <StreamActivityIndicator
      testId="tool-tail-activity"
      className="mt-3"
      label={compressing ? '压缩中' : '执行中'}
      anchorMs={streamAnchorMs}
    />
  )
}
