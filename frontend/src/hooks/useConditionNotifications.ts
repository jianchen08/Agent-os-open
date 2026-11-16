/**
 * 条件态 → 通知中心同步引擎
 *
 * 把「持续型条件」（连接断开/预算超限/插件不可用等）同步为通知中心的条目：
 * - 条件活跃 → 通知在列（同 episode 幂等 upsert：同 id 重复入列由 store 更新
 *   而非新增，badge 不增长）
 * - 条件解除 → 条目保留 resolveHoldMs 后移除（缓冲解除瞬间的抖动：快速
 *   断连-重连不重复出条目）；保留期内条件复燃 → 取消移除、沿用同一条目
 *   （同 episode 继续，不双条）
 * - 条件解除收口 episode：同键再次活跃生成新 episode id——用户上一轮关闭过
 *   的 id 由 store 已移除 id 记忆挡回弹，新 id 不受影响（每轮新条件都能再次
 *   提醒）
 *
 * id 形如 `${kind}#${episode}`：kind 为条件稳定键，episode 从 1 递增。
 * 通知样式/可关闭性由 NotificationCenter 现有面承载（critical=红/high=警示，
 * X 关闭），本引擎零 DOM。
 */
import { useEffect, useRef } from 'react'
import { useNotificationStore } from '@/stores/notificationStore'
import type { NotificationItem } from '@/types/notification'

/** 单个持续型条件的通知描述（id 由引擎按 episode 生成，生产方不填） */
export interface ConditionNotification {
  /** 条件稳定键（episode id 命名空间；同键跨轮次共享 episode 计数） */
  kind: string
  /** 通知载荷（除 id/isRead/timestamp 外的字段） */
  notification: Omit<NotificationItem, 'id' | 'isRead' | 'timestamp'>
}

/** 条件解除后条目保留时长（与原顶栏横幅解除保留期一致） */
export const DEFAULT_RESOLVE_HOLD_MS = 4000

/**
 * 同步一组持续型条件到通知中心（挂载期常驻调用；条件数组变化自动对账）。
 *
 * @param conditions 当前活跃条件集（全量；引擎按 kind 对账增删）
 * @param resolveHoldMs 条件解除后条目保留时长（测试可注入缩短）
 */
export function useConditionNotifications(
  conditions: ConditionNotification[],
  resolveHoldMs = DEFAULT_RESOLVE_HOLD_MS,
): void {
  /** 每条件键的 episode 计数（同键第 N 次活跃 = 第 N 轮提醒） */
  const episodeRef = useRef(new Map<string, number>())
  /** 活跃条件键 → 当前在列条目 id（解除即出表，保留期归属 holdTimersRef） */
  const activeIdRef = useRef(new Map<string, string>())
  /** 条件键 → 最近一次条目 id（保留期复燃沿用，避免双条） */
  const lastIdRef = useRef(new Map<string, string>())
  /** 保留期定时器（条件键 → 解除移除定时器） */
  const holdTimersRef = useRef(new Map<string, ReturnType<typeof setTimeout>>())

  useEffect(() => {
    const store = useNotificationStore.getState()
    const activeKinds = new Set(conditions.map((c) => c.kind))

    for (const cond of conditions) {
      // 保留期内复燃：同 episode 继续（沿用上一条目 id），取消解除移除
      const pendingTimer = holdTimersRef.current.get(cond.kind)
      const wasHolding = pendingTimer !== undefined
      if (pendingTimer) {
        clearTimeout(pendingTimer)
        holdTimersRef.current.delete(cond.kind)
      }

      let id = activeIdRef.current.get(cond.kind)
      if (!id && wasHolding) id = lastIdRef.current.get(cond.kind)
      if (!id) {
        const episode = (episodeRef.current.get(cond.kind) ?? 0) + 1
        episodeRef.current.set(cond.kind, episode)
        id = `${cond.kind}#${episode}`
      }
      activeIdRef.current.set(cond.kind, id)
      lastIdRef.current.set(cond.kind, id)
      store.addNotification({ ...cond.notification, id })
    }

    // 已解除条件：保留 resolveHoldMs 后移除条目并收口 episode
    for (const [kind, id] of [...activeIdRef.current]) {
      if (activeKinds.has(kind)) continue
      activeIdRef.current.delete(kind)
      const timer = setTimeout(() => {
        holdTimersRef.current.delete(kind)
        useNotificationStore.getState().removeNotification(id)
      }, resolveHoldMs)
      holdTimersRef.current.set(kind, timer)
    }
  }, [conditions, resolveHoldMs])

  // 卸载清场：本实例在列/保留期内的条目全部撤下（同步属主消失即条目消失；
  // 直接过滤不走 removeNotification——那是用户移除语义，会把 id 记成已关闭，
  // 重挂后同 episode 的合法重报被误挡）。episode 计数一并复位：重挂即新账本。
  // 取舍（P-144，有意保持零行为改动）：绕过已移除记忆意味着重挂后同条件重报
  // 会以未读再入列，badge 对同一条件可能重复计数——同步属主重新在场时重新
  // 提醒是合法语义，误挡真提醒比 badge 重复计数代价更高，此处仅明示不改动。
  useEffect(() => {
    const timers = holdTimersRef.current
    const activeIds = activeIdRef.current
    const lastIds = lastIdRef.current
    const episodes = episodeRef.current
    return () => {
      for (const timer of timers.values()) clearTimeout(timer)
      timers.clear()
      const owned = new Set(activeIds.values())
      if (owned.size > 0) {
        useNotificationStore.setState((state) => ({
          notifications: state.notifications.filter((n) => !owned.has(n.id)),
        }))
      }
      activeIds.clear()
      lastIds.clear()
      episodes.clear()
    }
  }, [])
}
