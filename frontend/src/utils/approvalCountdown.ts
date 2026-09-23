/**
 * 审批等待倒计时纯工具
 *
 * 消费方：InteractionCard 卡内倒计时（BUG-14 有界等待可见化）、
 * GlobalInteractionOverlay 收起徽标倒计时（BUG-43）。
 */
import type { PendingInteraction } from '@/stores/interactionStore'

/**
 * 审批等待截止时刻（ms）；时间戳缺失或不可解析返回 null。
 *
 * 直接消费交互事件声明的 timeout_seconds（BUG-60 用户裁定 2026-09-22：审批族
 * 超时统一 24h——内核 mcp client 默认 86400s 与 human create_choice 声明对齐，
 * 声明值端到端生效，不再有 300s 隐性决策窗，min cap 已废除）。
 * createdAt 缺失时回退用「卡片到达前端时刻」近似基准——到达时延会略缩短显示窗（妥协，见 interactionStore.createdAt）。
 */
export function approvalDeadlineMs(
  interaction: Pick<PendingInteraction, 'createdAt' | 'timestamp' | 'timeoutSeconds'>,
): number | null {
  const base = Date.parse(interaction.createdAt || interaction.timestamp || '')
  if (!Number.isFinite(base)) return null
  return base + (interaction.timeoutSeconds ?? 0) * 1000
}

/** 剩余时间格式：<1h 为 m:ss；≥1h 为 h:mm:ss */
export function formatRemaining(totalSeconds: number): string {
  const h = Math.floor(totalSeconds / 3600)
  const m = Math.floor((totalSeconds % 3600) / 60)
  const s = totalSeconds % 60
  if (h > 0) return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
  return `${m}:${String(s).padStart(2, '0')}`
}
