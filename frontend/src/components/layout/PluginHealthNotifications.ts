/**
 * 插件不可用降级通知 · 通知中心承载（原 PluginHealthBanner 顶栏横幅并入右侧
 * 实时通知面，卡3 前端工单，装机 2026-10-17 事故：用户零感知的教训——错误
 * 透传的终点是用户，不是日志文件）。
 *
 * 数据源=事件驱动本地维持（pluginHealthStore）：后端三路判定源（spawn 熔断
 * 跳闸 / 合宿成员下线 / G2 verify_failed 跨线）经 system_notification 推不可用
 * 通知（record_id 前缀 n_plugin_unavailable_，见 kernel plugin_notices），前端
 * 登记降级项；persist 落 localStorage 防刷新丢失（内核侧同类事件 10 分钟去重窗
 * + verify_failed 粘滞，刷新后不会立刻重报）。每条未恢复降级项 = 通知中心一条
 * 红色条目（可关闭；关闭只隐藏本轮，恢复后同键再坏以新 episode 重报）。
 *
 * 恢复对账：后端恢复侧无 WS 事件（转正只清内核内存账本），三路升级路径在
 * verify_failed 终态汇合——消费既有只读读面 GET /api/v1/plugins/contract-status
 * （复用 parseContractStatus 解析），账本非空且无 verify_failed → 清全部降级项
 * （保守语义）；读不到/为空保留。仅在降级项非空时轮询（空转零请求）。
 */
import { useEffect, useMemo } from 'react'
import { parseContractStatus } from '@/components/debug/ContractStatusPanel'
import { API_ENDPOINTS } from '@/constants/api'
import { useConditionNotifications, type ConditionNotification } from '@/hooks/useConditionNotifications'
import apiClient from '@/services/api/client'
import { usePluginHealthStore, type PluginHealthEntry } from '@/stores/pluginHealthStore'
import { loggers } from '@/utils/logger'
import type { NotificationPriority } from '@/types/notification'

/** 拉取契约读面并对账（账本空/读失败保留降级项，由 store 保守语义兜底） */
async function reconcileOnce(store: ReturnType<typeof usePluginHealthStore.getState>): Promise<void> {
  try {
    const resp = await apiClient.get<unknown>(API_ENDPOINTS.PLUGINS.CONTRACT_STATUS)
    store.reconcileWithContractStatuses(parseContractStatus(resp.data))
  } catch (err) {
    // 读面不可用≠恢复：保留降级项，下轮重试
    loggers.websocket.debug('[plugin-health] 恢复对账读面不可用，保留降级项: %s', String(err))
  }
}

/** 恢复对账轮询周期（低频兜底：内核 10 分钟通知去重窗的量级之下即可） */
export const PLUGIN_HEALTH_RECONCILE_INTERVAL_MS = 60_000

/** 降级项行动指引（P-25）：告知重启应用与反馈日志两条出路 */
const ENTRY_ACTION_HINT = '保存后改动无效时请重启应用；多次失败请反馈日志。'

/** 后端 level → 通知优先级（error 族=红色 critical；warn 族=high 警示；其余 high 兜底） */
export function priorityForLevel(level: string): NotificationPriority {
  if (level === 'warning' || level === 'warn') return 'high'
  return 'critical'
}

/**
 * 降级项 → 通知条目载荷（合并一核心映射：红色错误条目 + 可关闭 +
 * 行动指引随正文呈现）。
 */
export function pluginHealthEntryToNotificationInput(
  entry: PluginHealthEntry,
): ConditionNotification['notification'] {
  return {
    title: `${entry.subject}不可用`,
    message: [entry.content, ENTRY_ACTION_HINT].join('\n\n'),
    category: 'error',
    priority: priorityForLevel(entry.level),
    isBlocking: false,
    sourceLabel: '插件系统',
  }
}

/**
 * 插件不可用 → 通知中心同步 + 恢复对账（合并一挂载点）：应用根组件常驻调用。
 * 每条未恢复降级项一条通知（kind=entry.key 承载 episode 命名空间）；全部恢复
 * 即撤（对账清账后条目随条件解除延迟移除）。
 */
export function usePluginHealthNotifications(resolveHoldMs?: number): void {
  const unavailable = usePluginHealthStore((s) => s.unavailable)

  // 恢复对账轮询：仅降级项在场时轮询（空转零请求）；挂载即先对账一次
  const hasDegraded = unavailable.length > 0
  useEffect(() => {
    if (!hasDegraded) return
    const store = usePluginHealthStore.getState()
    void reconcileOnce(store)
    const timer = window.setInterval(() => {
      void reconcileOnce(usePluginHealthStore.getState())
    }, PLUGIN_HEALTH_RECONCILE_INTERVAL_MS)
    return () => window.clearInterval(timer)
  }, [hasDegraded])

  const conditions = useMemo(
    () =>
      unavailable.map(
        (entry): ConditionNotification => ({
          kind: `plugin-health:${entry.key}`,
          notification: pluginHealthEntryToNotificationInput(entry),
        }),
      ),
    [unavailable],
  )

  useConditionNotifications(conditions, resolveHoldMs)
}
