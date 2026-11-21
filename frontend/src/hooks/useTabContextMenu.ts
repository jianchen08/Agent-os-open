/**
 * useTabContextMenu —— 标签右键菜单 + 批量关签确认层（两条标签栏共用状态机）
 *
 * 状态机（与交互语义一并收敛，避免两处各写一套）：
 * 1. 右键标签 → 菜单开（定位光标，记录目标签固定态）；
 * 2. 「关闭本标签」→ 收起菜单，直接关单签（可逆，不拦）；
 * 3. 「关闭其他标签 / 关闭所有标签」→ 收起菜单；影响面为 0（其余全是固定/
 *    不可关签）时动作本身是 no-op，直接执行不带确认；否则挂起确认层，
 *    确认后才落关签（BUG-79 实证入口防误触契约，两栏一致）。
 *
 * 纯判定（哪些签保留、影响几个）在 `@/utils/tabCloseSemantics`，本 hook 只管
 * 状态流转；菜单呈现与确认层呈现分别是 TabContextMenu / TabBulkCloseDialog。
 */

import { useCallback, useState, type MouseEvent } from 'react'
import {
  bulkCloseCount,
  type TabBulkCloseAction,
  type TabClosability,
} from '@/utils/tabCloseSemantics'
import type { TabBulkCloseRequest } from '@/components/layout/TabBulkCloseDialog'

/** 菜单定位与目标签 */
export interface TabContextMenuTarget {
  x: number
  y: number
  tabId: string
  isPinned: boolean
  /** 业务可关（canClose !== false） */
  canClose: boolean
}

export interface UseTabContextMenuParams<T extends TabClosability> {
  /** 当前标签集合（计算批量关签影响面） */
  tabs: readonly T[]
  /** 关闭本标签 */
  onCloseTab: (tabId: string) => void
  /** 关闭其他标签（基准签与不可关签保留） */
  onCloseOtherTabs: (tabId: string) => void
  /** 关闭全部标签（不可关签保留） */
  onCloseAllTabs: () => void
}

export function useTabContextMenu<T extends TabClosability>({
  tabs,
  onCloseTab,
  onCloseOtherTabs,
  onCloseAllTabs,
}: UseTabContextMenuParams<T>) {
  const [menu, setMenu] = useState<TabContextMenuTarget | null>(null)
  const [bulkClose, setBulkClose] = useState<TabBulkCloseRequest | null>(null)

  /** 开菜单：定位于光标并记录目标签的关闭语义 */
  const openMenu = useCallback((e: MouseEvent, tab: T) => {
    e.preventDefault()
    e.stopPropagation()
    setMenu({
      x: e.clientX,
      y: e.clientY,
      tabId: tab.id,
      isPinned: !!tab.isPinned,
      canClose: tab.canClose !== false,
    })
  }, [])

  const closeMenu = useCallback(() => setMenu(null), [])

  /** 单签关闭：收起菜单后立即执行 */
  const requestClose = useCallback(
    (tabId: string) => {
      setMenu(null)
      onCloseTab(tabId)
    },
    [onCloseTab],
  )

  /** 批量关签：影响面为 0 直接执行，否则挂起确认层 */
  const requestBulkClose = useCallback(
    (action: TabBulkCloseAction, tabId: string) => {
      setMenu(null)
      const count = bulkCloseCount(tabs, action, tabId)
      if (count === 0) {
        if (action === 'closeOther') onCloseOtherTabs(tabId)
        else onCloseAllTabs()
        return
      }
      setBulkClose({ action, tabId, count })
    },
    [tabs, onCloseOtherTabs, onCloseAllTabs],
  )

  /** 确认层确认：执行挂起的批量关签 */
  const confirmBulkClose = useCallback(() => {
    if (!bulkClose) return
    if (bulkClose.action === 'closeOther') onCloseOtherTabs(bulkClose.tabId)
    else onCloseAllTabs()
    setBulkClose(null)
  }, [bulkClose, onCloseOtherTabs, onCloseAllTabs])

  /** 确认层取消：不执行任何关签 */
  const cancelBulkClose = useCallback(() => setBulkClose(null), [])

  return {
    menu,
    openMenu,
    closeMenu,
    requestClose,
    requestBulkClose,
    bulkClose,
    confirmBulkClose,
    cancelBulkClose,
  }
}
