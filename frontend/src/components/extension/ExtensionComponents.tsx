/**
 * ExtensionComponents（P5-a/b 渲染层，ADR §3.4 档位二）
 *
 * - CommandPalette：命令面板（Cmd/Ctrl+Shift+P 打开），搜索 + 执行 + when 过滤
 * - ContextMenuItems：右键/上下文菜单项（按 location + when 过滤）
 *
 * 模态弹窗呈现已收敛至 shell.overlay 槽锚点（SlotOverlayHost，C4 槽位机制；
 * contributes.modal → shell.overlay 域条目，触发面 commands 不动）。
 * 所有命令触发统一经 CommandDispatcher.executeCommand（→ 内核 transport 或
 * 打开声明的 overlay）。
 */

import React, { useEffect, useState } from 'react'
import type { CommandDispatcher } from '@/services/schema/commandDispatcher'

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
                  className="hover:bg-muted flex w-full items-center gap-2 px-4 py-2 text-left text-sm"
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
            className="hover:bg-muted flex w-full items-center px-3 py-1.5 text-left text-sm"
            onClick={() => void dispatcher.executeCommand(item.command as string)}
          >
            {item.title || item.id}
          </button>
        </li>
      ))}
    </ul>
  )
}

// ExtensionModalHost + useExtensionModal 已删除（存量收敛：modal 呈现面迁至
// components/slots/SlotOverlayHost，C4 槽位机制统一承载 widget/webview 通道）
