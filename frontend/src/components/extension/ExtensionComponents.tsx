/**
 * ExtensionComponents（P5-a/b/d 渲染层，ADR §3.4 档位二）
 *
 * - CommandPalette：命令面板（Cmd/Ctrl+Shift+P 打开），搜索 + 执行 + when 过滤
 * - ContextMenuItems：右键/上下文菜单项（按 location + when 过滤）
 * - ExtensionModalHost + useExtensionModal：模态弹窗（command 触发，用声明的预置 widget 渲染）
 *
 * 所有命令触发统一经 CommandDispatcher.executeCommand（→ 内核 transport 或打开 modal）。
 * modal 的 widget 只能引用预置 widget（ADR §2.3），由外部通过 children 注入渲染。
 */

import React, { useEffect, useState } from 'react'
import type { CommandDispatcher } from '@/services/schema/commandDispatcher'
import type { ContributionEntry } from '@/services/schema/ContributionRegistry'

// ──────────────────────────────────────────────────────────────
// CommandPalette（P5-b 命令面板）
// ──────────────────────────────────────────────────────────────

interface CommandPaletteProps {
  /** 是否打开 */
  open: boolean
  /** 命令分发器 */
  dispatcher: CommandDispatcher
  /** 关闭回调 */
  onClose: () => void
}

/**
 * 命令面板：搜索可见命令并执行
 *
 * 打开时列出所有 when 命中的命令；输入关键词过滤；点击执行并关闭。
 */
export function CommandPalette({ open, dispatcher, onClose }: CommandPaletteProps): React.ReactElement | null {
  const [query, setQuery] = useState('')

  // 关闭时重置查询
  useEffect(() => {
    if (!open) setQuery('')
  }, [open])

  if (!open) return null

  const visible = dispatcher.getVisibleCommands()
  const filtered = query.trim() ? dispatcher.searchCommands(query).filter(
    (c) => visible.some((v) => v.id === c.id),
  ) : visible

  const handleSelect = (commandId: string): void => {
    // 先关闭面板（即时反馈），命令异步触发（fire-and-forget）
    onClose()
    void dispatcher.executeCommand(commandId)
  }

  return (
    <div
      className="fixed inset-0 z-[200] flex items-start justify-center bg-[var(--overlay-bg)] pt-24"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      data-testid="command-palette"
    >
      <div
        className="bg-popover text-popover-foreground w-full max-w-lg rounded-lg border shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <input
          className="border-border w-full border-b px-4 py-3 text-sm outline-none"
          placeholder="搜索命令..."
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          autoFocus
        />
        <ul className="max-h-80 overflow-y-auto py-1">
          {filtered.length === 0 ? (
            <li className="text-muted-foreground px-4 py-2 text-sm">无匹配命令</li>
          ) : (
            filtered.map((cmd) => (
              <li key={cmd.id}>
                <button
                  className="hover:bg-accent flex w-full items-center gap-2 px-4 py-2 text-left text-sm"
                  onClick={() => handleSelect(cmd.id)}
                >
                  {cmd.category && (
                    <span className="text-muted-foreground text-xs">{cmd.category}</span>
                  )}
                  <span>{cmd.title || cmd.id}</span>
                </button>
              </li>
            ))
          )}
        </ul>
      </div>
    </div>
  )
}

// ──────────────────────────────────────────────────────────────
// ContextMenuItems（P5-a 右键/上下文菜单）
// ──────────────────────────────────────────────────────────────

interface ContextMenuItemsProps {
  /** 菜单位置（如 'workspace/context'、'chat/context'） */
  location: string
  /** 命令分发器 */
  dispatcher: CommandDispatcher
}

/**
 * 渲染指定位置的右键菜单项（已按 location + when 过滤）。
 * 父级（右键菜单容器）负责定位/显隐；本组件只渲染菜单项列表。
 */
export function ContextMenuItems({ location, dispatcher }: ContextMenuItemsProps): React.ReactElement {
  const items = dispatcher.getVisibleMenus(location)
  return (
    <ul className="min-w-[160px] py-1" data-testid={`context-menu-${location}`}>
      {items.map((item) => (
        <li key={item.id}>
          <button
            className="hover:bg-accent flex w-full items-center px-3 py-1.5 text-left text-sm"
            onClick={() => void dispatcher.executeCommand(item.command as string)}
          >
            {item.title || item.id}
          </button>
        </li>
      ))}
    </ul>
  )
}

// ──────────────────────────────────────────────────────────────
// ExtensionModalHost + useExtensionModal（P5-d 模态弹窗）
// ──────────────────────────────────────────────────────────────

interface ModalState {
  /** 当前待显示的 modal（null 表示无） */
  modal: ContributionEntry | null
  /** 关闭当前 modal */
  closeModal: () => void
}

/**
 * 订阅 CommandDispatcher 的 modal 打开事件，返回当前待显示的 modal。
 *
 * 命令触发后若存在 trigger=on_command:<commandId> 的 modal 声明，
 * CommandDispatcher 会广播打开事件，本 hook 捕获并设为当前 modal。
 */
export function useExtensionModal(dispatcher: CommandDispatcher): ModalState {
  const [modal, setModal] = useState<ContributionEntry | null>(null)

  useEffect(() => {
    return dispatcher.onModalOpen((m) => setModal(m))
  }, [dispatcher])

  return { modal, closeModal: () => setModal(null) }
}

interface ExtensionModalHostProps {
  /** 待显示的 modal 声明 */
  modal: ContributionEntry
  /** 关闭回调 */
  onClose: () => void
  /** modal 内容（由父级用声明的预置 widget 渲染后注入） */
  children: React.ReactNode
}

/**
 * 模态弹窗容器：渲染遮罩 + 标题栏 + 内容区。
 *
 * modal 的 widget 由父级查 widget registry 后作为 children 注入（ADR §2.3：
 * 插件不能贡献任意 React 组件，只能引用预置 widget）。
 */
export function ExtensionModalHost({ modal, onClose, children }: ExtensionModalHostProps): React.ReactElement {
  return (
    <div
      className="fixed inset-0 z-[200] flex items-center justify-center bg-[var(--overlay-bg)]"
      onClick={onClose}
      role="dialog"
      aria-modal="true"
      data-testid="extension-modal"
    >
      <div
        className="bg-popover text-popover-foreground w-full max-w-2xl rounded-lg border shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-border flex items-center justify-between border-b px-4 py-3">
          <h2 className="text-base font-medium">{modal.title || modal.id}</h2>
          <button
            className="text-muted-foreground hover:text-foreground px-2 text-lg"
            onClick={onClose}
            aria-label="关闭"
          >
            ×
          </button>
        </div>
        <div className="max-h-[70vh] overflow-y-auto p-4">{children}</div>
      </div>
    </div>
  )
}
