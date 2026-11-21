/**
 * 标签关闭语义（对话标签与工作区标签共用的纯函数单源）
 *
 * 两类标签栏的可关闭性由两个正交字段决定：
 * - `isPinned`：用户固定——固定签不受批量关签影响，也不给单签关闭入口；
 * - `canClose`：业务不可关（对话主标签是会话锚点，关掉即丢锚）——工作区
 *   标签无此字段，缺省视为可关。
 *
 * 判定收在这里，避免两条标签栏各写一套（复制必漂移）：工作区的
 * `filter(t => t.isPinned)` 与对话流的产签规则在同一条函数上对齐后，
 * 后加第三类标签栏也不会有第二套口径。
 */

/** 可判定关闭语义的标签最小形状 */
export interface TabClosability {
  id: string
  /** 用户固定（钉住）：固定签批量关签保留、无单签关闭入口 */
  isPinned?: boolean
  /** 业务不可关；缺省 true（可关）——工作区标签无此字段 */
  canClose?: boolean
}

/** 批量关签动作 */
export type TabBulkCloseAction = 'closeOther' | 'closeAll'

/** 单签可关闭：未固定且业务允许关闭 */
export function isTabClosable(tab: TabClosability): boolean {
  return tab.canClose !== false && !tab.isPinned
}

/**
 * 批量关签后必须保留的标签 id 集合：
 * - `closeOther`：基准签（targetTabId）+ 不可关签；
 * - `closeAll`：不可关签。
 * 不可关签 = 固定签 ∪ 业务禁关签（如对话主标签）。
 */
export function keepSetAfterBulkClose(
  tabs: readonly TabClosability[],
  action: TabBulkCloseAction,
  targetTabId?: string,
): Set<string> {
  const keep = new Set<string>()
  for (const tab of tabs) {
    if (!isTabClosable(tab) || (action === 'closeOther' && tab.id === targetTabId)) {
      keep.add(tab.id)
    }
  }
  return keep
}

/** 批量关签影响面（将被关闭的标签数；0 = 动作本身是 no-op，不必出确认层） */
export function bulkCloseCount(
  tabs: readonly TabClosability[],
  action: TabBulkCloseAction,
  targetTabId?: string,
): number {
  const keep = keepSetAfterBulkClose(tabs, action, targetTabId)
  return tabs.reduce((count, tab) => (keep.has(tab.id) ? count : count + 1), 0)
}
