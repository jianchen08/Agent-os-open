/**
 * ExtensionHost（P5 集成挂载点，ADR §3.4 档位二）
 *
 * 在应用根挂载一次，统一装配 contributes.* 的运行时：
 * - useExtensionShortcuts：注册全局快捷键（contributes.shortcuts）
 * - CommandPalette：Cmd/Ctrl+Shift+P 打开（聚合 contributes.commands）
 * - SlotOverlayHost：shell.overlay 槽锚点（C4 槽位机制——contributes.modal
 *   收敛为 shell.overlay 域条目，呈现=模态；触发面 commands 不动。
 *   设计真值 = docs/working/DSH生态嫁接通用机制设计_20261104.md §4.4）
 *
 * 命令面板的打开快捷键固定为 Cmd/Ctrl+Shift+P（VS Code 习惯）。
 * overlay 内容渲染（widget/webview 通道、fail-closed、ErrorBoundary）由
 * SlotEntryContent/SlotRegistry 统一承载。
 */

import React, { useEffect, useState } from 'react'
import { SlotOverlayHost } from '@/components/slots/SlotOverlayHost'
import { useExtensionShortcuts } from '@/hooks/useExtensionShortcuts'
import { commandDispatcher } from '@/services/schema/commandDispatcher'
import { CommandPalette } from './ExtensionComponents'
import type { CommandDispatcher } from '@/services/schema/commandDispatcher'

interface ExtensionHostProps {
  /** 命令分发器（默认全局单例，可注入便于测试） */
  dispatcher?: CommandDispatcher
}

/** 平台判定：Mac 用 Cmd，其他用 Ctrl */
function isMac(): boolean {
  if (typeof navigator === 'undefined') return false
  return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)
}

export function ExtensionHost({ dispatcher = commandDispatcher }: ExtensionHostProps): React.ReactElement {
  // 1. 注册全局快捷键（contributes.shortcuts）
  useExtensionShortcuts()

  // 2. 命令面板开关（Cmd/Ctrl+Shift+P）
  const [paletteOpen, setPaletteOpen] = useState(false)

  useEffect(() => {
    const handleKeyDown = (ev: KeyboardEvent): void => {
      const want = isMac() ? ev.metaKey : ev.ctrlKey
      if (want && ev.shiftKey && (ev.key === 'P' || ev.key === 'p')) {
        ev.preventDefault()
        setPaletteOpen((prev) => !prev)
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [])

  return (
    <>
      <CommandPalette
        open={paletteOpen}
        dispatcher={dispatcher}
        onClose={() => setPaletteOpen(false)}
      />
      {/* shell.overlay 槽锚点（modal 收敛呈现；无触发零渲染） */}
      <SlotOverlayHost dispatcher={dispatcher} />
    </>
  )
}
