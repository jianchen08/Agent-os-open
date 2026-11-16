/**
 * 插件健康降级状态 Store（卡3 前端工单，装机 2026-10-17 事故）
 *
 * 状态源=事件驱动本地维持：后端三路判定源（spawn 熔断跳闸 / 合宿成员下线 /
 * G2 verify_failed 跨线）经 system_notification 推不可用通知（record_id 前缀
 * n_plugin_unavailable_，见 kernel plugin_notices），前端登记降级项；persist
 * 落 localStorage 防刷新丢失（内核侧同类事件 10 分钟去重窗 + verify_failed
 * 粘滞，刷新后不会立刻重报）。
 *
 * 呈现面=通知中心（usePluginHealthNotifications 同步，每条未恢复降级项一条
 * 红色条目）；条目的用户关闭由通知中心已移除 id 记忆承载（同轮不回弹，恢复
 * 后同键再坏以新 episode 重报），本 store 只管降级项账本。
 *
 * 恢复侧无事件：转正只清内核内存账本，不发 WS 通知。清除消费既有只读读面
 * GET /api/v1/plugins/contract-status——三路升级路径在 verify_failed 终态汇合
 * （台账 R6），账本非空且无任何 verify_failed 即「全部转正」→ 清全部降级项
 * （保守语义）；账本为空/读不到则保留（不能确认恢复就不撤条目）。
 */
import { create } from 'zustand'
import { persist } from 'zustand/middleware'

/** 一条「插件（组）当前不可用」降级项 */
export interface PluginHealthEntry {
  /** 去重键（kind|正文主语），对齐后端 kind+group+members 去重语义 */
  key: string
  /** 判定种类：spawn_breaker / cohost_member_offline / verify_failed */
  kind: string
  /** 正文主语（「当前不可用」前缀，如「插件组 light 成员 task_submit」） */
  subject: string
  /** 通知全文（含根因/下线工具数四要素） */
  content: string
  /** 通知级别（后端载荷 level） */
  level: string
  /** 首见时间（ISO 8601，upsert 保持首见） */
  firstSeenAt: string
}

/** 契约状态最小结构面（只消费 g2_consistency；完整面见 ContractStatusPanel） */
export interface ContractStatusLite {
  plugin_id?: string
  gates?: { g2_consistency?: string }
}

/** 判定不可用的 G2 终态（kernel contract.rs verify_failed） */
const VERIFY_FAILED = 'verify_failed'

interface PluginHealthState {
  /** 未恢复的插件不可用降级项（通知中心条目数据源） */
  unavailable: PluginHealthEntry[]
  /** 登记降级项（同键 upsert：正文/级别取最新，firstSeenAt 保持首见） */
  reportUnavailable: (entry: Omit<PluginHealthEntry, 'firstSeenAt'>) => void
  /** 清空全部降级项（全部恢复） */
  clearAllDegraded: () => void
  /** 契约读面对账：账本非空且无 verify_failed → 清全部；否则保留 */
  reconcileWithContractStatuses: (statuses: ContractStatusLite[]) => void
}

export const usePluginHealthStore = create<PluginHealthState>()(
  persist(
    (set) => ({
      unavailable: [],

      reportUnavailable: (entry) => {
        set((state) => {
          const existing = state.unavailable.find((e) => e.key === entry.key)
          if (existing) {
            return {
              unavailable: state.unavailable.map((e) =>
                e.key === entry.key
                  ? { ...e, content: entry.content, level: entry.level }
                  : e,
              ),
            }
          }
          return {
            unavailable: [
              ...state.unavailable,
              { ...entry, firstSeenAt: new Date().toISOString() },
            ],
          }
        })
      },

      clearAllDegraded: () => {
        set({ unavailable: [] })
      },

      reconcileWithContractStatuses: (statuses) => {
        if (statuses.length === 0) return // 账本空/读不到：保守保留
        const anyFailed = statuses.some((s) => s.gates?.g2_consistency === VERIFY_FAILED)
        if (anyFailed) return
        set({ unavailable: [] })
      },
    }),
    {
      name: 'agentos-plugin-health',
      version: 1,
      partialize: (state) => ({
        unavailable: state.unavailable,
      }),
    },
  ),
)
