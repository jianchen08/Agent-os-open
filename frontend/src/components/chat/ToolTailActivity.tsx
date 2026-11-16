/**
 * 工具尾部活性指示（2026-10-19 用户裁决）：流式尾消息的 parts 尾部停在
 * tool_call 时，无论该工具是否执行完都在卡下保持转圈——工具执行中与
 * 「结果已回、等 LLM 下一轮推送」的间隙统一显示执行中，执行不再有"静止段"。
 * 模型新一轮推送一旦到达即切换思考中（block_start 就建空 text/thinking part，
 * 早于任何可见内容，正好作切换信号）；可见内容（思考/正文非空）开始流式后
 * 指示行退场——内容本身即在动。审批等待态不占：等待用户归交互卡承载，
 * 此时「执行中」是误报。图标/耗时行统一走 StreamActivityIndicator
 * （2026-11-06 用户裁定）：总耗时自触发消息发出起计。
 */
import { StreamActivityIndicator } from './StreamActivityIndicator'
import type { MessagePart } from '@/types/messageParts'
import type { ReactNode } from 'react'

export function ToolTailActivity({
  streaming,
  parts,
  pendingInteraction,
  compressing,
  streamAnchorMs,
}: {
  streaming: boolean
  parts?: MessagePart[]
  pendingInteraction: boolean
  /** 上下文压缩中：压缩是分钟级同步调用，期间管道无流式产出——指示文案切
   *  「压缩中」，让用户看得出当前在等压缩而非等模型（用户裁定 2026-10-28）。 */
  compressing?: boolean
  /** 实时总耗时锚点（触发消息发出时刻 ms）；缺席不显示耗时 */
  streamAnchorMs?: number | null
}): ReactNode {
  if (!streaming || pendingInteraction) return null
  const list = parts ?? []
  let lastToolCallIdx = -1
  for (let i = list.length - 1; i >= 0; i--) {
    if (list[i].type === 'tool_call') {
      lastToolCallIdx = i
      break
    }
  }
  if (lastToolCallIdx < 0) return null
  const afterTool = list.slice(lastToolCallIdx + 1)
  const modelPushArrived = afterTool.some((p) => p.type === 'text' || p.type === 'thinking')
  const visibleContentAfterTool = afterTool.some(
    (p) => (p.type === 'text' || p.type === 'thinking') && p.content.trim() !== '',
  )
  if (visibleContentAfterTool) return null
  const label = compressing ? '压缩中...' : modelPushArrived ? '思考中...' : '执行中...'
  return (
    <StreamActivityIndicator
      testId="tool-tail-activity"
      className="mt-3"
      label={label}
      anchorMs={streamAnchorMs}
    />
  )
}
