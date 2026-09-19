/**
 * 审批等待倒计时纯工具
 *
 * 消费方：InteractionCard 卡内倒计时（BUG-14 有界等待可见化）、
 * GlobalInteractionOverlay 收起徽标倒计时（BUG-43）。
 */
import type { PendingInteraction } from '@/stores/interactionStore'

/** 审批等待截止时刻（ms）；时间戳缺失或不可解析返回 null */
export function approvalDeadlineMs(
  interaction: Pick<PendingInteraction, 'createdAt' | 'timestamp' | 'timeoutSeconds'>,
): number | null {
  const base = Date.parse(interaction.createdAt || interaction.timestamp || '')
  if (!Number.isFinite(base)) return null
  return base + (interaction.timeoutSeconds ?? 0) * 1000
}

/** 剩余时间格式：<1h 为 m:ss；≥1h 为 h:mm:ss（BUG-40 24h 等待上限可读展示） */
export function formatRemaining(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600)
  const m = Math.floor((totalSeconds % 3600) / 60)
  const s = totalSeconds % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return `${m}:${String(s).padStart(2, '0')}`
}
