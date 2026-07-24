/**
 * ExtensionHost（P5 集成挂载点，ADR §3.4 档位二）
 *
 * 在应用根挂载一次，统一装配 contributes.* 的运行时：
 * - useExtensionShortcuts：注册全局快捷键（contributes.shortcuts）
 * - CommandPalette：Cmd/Ctrl+Shift+P 打开（聚合 contributes.commands）
 * - ExtensionModalHost：命令触发的模态弹窗（contributes.modal）
 *
 * 命令面板的打开快捷键固定为 Cmd/Ctrl+Shift+P（VS Code 习惯）。
 * modal 的 widget 渲染由 widgetRegistry 提供（预置 widget，ADR §2.3）。
 */

import React, { useEffect, useState } from 'react'
import { CommandPalette, ExtensionModalHost, useExtensionModal } from './ExtensionComponents'
import { useExtensionShortcuts } from '@/hooks/useExtensionShortcuts'
import { commandDispatcher } from '@/services/schema/commandDispatcher'
import type { CommandDispatcher } from '@/services/schema/commandDispatcher'

/** 预置 widget 渲染器签名 */
export type WidgetRenderer = (
  props: Record<string, unknown>,
  onClose: () => void,
) => React.ReactNode

/** 预置 widget 注册表（widget 名 → 渲染器） */
export type WidgetRegistry = Record<string, WidgetRenderer>

interface ExtensionHostProps {
  /** 预置 widget 注册表（决定 modal 能渲染哪些 widget，ADR §2.3） */
  widgetRegistry?: WidgetRegistry
  /** 命令分发器（默认全局单例，可注入便于测试） */
  dispatcher?: CommandDispatcher
}

/** 平台判定：Mac 用 Cmd，其他用 Ctrl */
function isMac(): boolean {
  if (typeof navigator === 'undefined') return false
  return /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent)
}

export function ExtensionHost({ widgetRegistry = {}, dispatcher = commandDispatcher }: ExtensionHostProps): React.ReactElement {
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

  // 3. modal 宿主（contributes.modal，命令触发）
  const { modal, closeModal } = useExtensionModal(dispatcher)

  const renderModalContent = (): React.ReactNode => {
    if (!modal) return null
    const widgetName = modal.widget as string | undefined
    if (!widgetName) return null
    const renderer = widgetRegistry[widgetName]
    if (!renderer) {
      return (
        <div className="text-muted-foreground text-sm">
          未知 widget: {widgetName}（需在前端预置 widget 注册表中声明）
        </div>
      )
    }
    return renderer((modal.props as Record<string, unknown>) ?? {}, closeModal)
  }

  return (
    <>
      <CommandPalette
        open={paletteOpen}
        dispatcher={dispatcher}
        onClose={() => setPaletteOpen(false)}
      />
      {modal && (
        <ExtensionModalHost modal={modal} onClose={closeModal}>
          {renderModalContent()}
        </ExtensionModalHost>
      )}
    </>
  )
}
