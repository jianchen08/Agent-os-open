/**
 * TabContextMenu —— 标签右键菜单（对话标签与工作区标签共用）
 *
 * 菜单项由调用方按 `buildTabMenuItems` 生成（同一条目顺序/文案/交互语义），
 * 本组件只负责呈现：定位于光标、点外部/Escape 关闭。菜单项集合的差异（如
 * 工作区页签的「弹出为浮窗」）以 `leadingItems` 追加，不改共用条目定义。
 *
 * 样式全部取自既有设计体系 token（bg-popover/text-popover-foreground/
 * hover:bg-muted/z-index 与 WorkPanel 原实现一致），不另造一套。
 */

import { useEffect, useRef, type ReactNode } from 'react'
import { cn } from '@/lib/utils'

/** 菜单项定义 */
export interface TabContextMenuItem {
  /** 稳定键（React key 与测试定位） */
  key: string
  /** UTF-8 中日韩文案 */
  label: string
  onSelect: () => void
  /** 禁用态（如固定签的「关闭本标签」） */
  disabled?: boolean
  /** 禁用原因（title 提示，禁用时必给，避免死入口） */
  disabledReason?: string
  /** 测试定位 id */
  testId?: string
}

export interface TabContextMenuProps {
  /** 光标位置（clientX/clientY） */
  x: number
  y: number
  /** 菜单条目前置自定义项（原生节点，工作区：弹出为浮窗） */
  leadingItems?: ReactNode
  items: TabContextMenuItem[]
  /** 关闭菜单（点外部 / Escape / 动作后由调用方收起） */
  onClose: () => void
}

/** 菜单项款式（弹出层 token；调用方注入前置条目时复用同一款式，不另写一套） */
export const TAB_MENU_ITEM_CLASS =
  'hover:bg-muted text-muted-foreground hover:text-foreground flex w-full items-center rounded ' +
  'px-2.5 py-1.5 text-left text-xs disabled:opacity-40'

export function TabContextMenu({ x, y, leadingItems, items, onClose }: TabContextMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) onClose()
    }
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', handleClickOutside)
    document.addEventListener('keydown', handleKey)
    return () => {
      document.removeEventListener('mousedown', handleClickOutside)
      document.removeEventListener('keydown', handleKey)
    }
  }, [onClose])

  return (
    <div
      ref={menuRef}
      role="menu"
      className={cn(
        'bg-popover text-popover-foreground fixed z-[100] min-w-[140px] rounded-lg border p-1 text-sm shadow-lg',
      )}
      style={{ left: x, top: y }}
    >
      {leadingItems}
      {items.map((item) => (
        <button
          key={item.key}
          type="button"
          role="menuitem"
          disabled={item.disabled}
          title={item.disabled ? item.disabledReason : undefined}
          className={TAB_MENU_ITEM_CLASS}
          data-testid={item.testId}
          onClick={item.onSelect}
        >
          {item.label}
        </button>
      ))}
    </div>
  )
}

/**
 * 生成共用菜单条目：固定/取消固定 → 关闭本标签 → 关闭其他标签 → 关闭所有标签。
 *
 * testId 前缀区分两条栏（`workspace-tab-menu` / `agent-tab-menu`），条目语义一致。
 */
export function buildTabMenuItems(params: {
  testIdPrefix: string
  isPinned: boolean
  /** 业务可关（canClose !== false）；固定签恒不可关 */
  canClose: boolean
  /** 未提供的动作 = 该能力在本栏不可用，条目不出（不给死入口） */
  onTogglePin?: () => void
  onClose: () => void
  onCloseOther?: () => void
  onCloseAll?: () => void
}): TabContextMenuItem[] {
  const closable = params.canClose && !params.isPinned
  const items: TabContextMenuItem[] = []
  if (params.onTogglePin) {
    items.push({
      key: 'pin',
      label: params.isPinned ? '取消固定' : '固定标签',
      testId: `${params.testIdPrefix}-pin`,
      onSelect: params.onTogglePin,
    })
  }
  items.push({
    key: 'close',
    label: '关闭本标签',
    testId: `${params.testIdPrefix}-close`,
    onSelect: params.onClose,
    disabled: !closable,
    disabledReason: params.isPinned
      ? '固定标签不可关闭'
      : params.canClose
        ? undefined
        : '主标签不可关闭',
  })
  if (params.onCloseOther) {
    items.push({
      key: 'closeOther',
      label: '关闭其他标签',
      testId: `${params.testIdPrefix}-close-other`,
      onSelect: params.onCloseOther,
    })
  }
  if (params.onCloseAll) {
    items.push({
      key: 'closeAll',
      label: '关闭所有标签',
      testId: `${params.testIdPrefix}-close-all`,
      onSelect: params.onCloseAll,
    })
  }
  return items
}
