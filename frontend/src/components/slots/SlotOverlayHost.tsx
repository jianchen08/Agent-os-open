/**
 * SlotOverlayHost — shell.overlay 槽锚点（C4，设计真值 =
 * docs/working/DSH生态嫁接通用机制设计_20261104.md §4.4 存量三面收敛）
 *
 * `contributes.modal` 收敛为 shell.overlay 域条目（呈现=模态）；触发面
 * （contributes.commands + commandDispatcher）是动作面不进槽——本组件订阅
 * commandDispatcher 的触发事件，按条目声明渲染模态内容（widget 通道经
 * SlotEntryContent，fail-closed/ErrorBoundary 契约与 SlotHost 同源）。
 */

import React, { useEffect, useState } from 'react'
import { commandDispatcher } from '@/services/schema/commandDispatcher'
import { SlotEntryContent } from './SlotHost'
import type { CommandDispatcher } from '@/services/schema/commandDispatcher'
import type { SlotEntry } from '@/services/slots/slotRegistry'

interface SlotOverlayHostProps {
  /** 命令分发器（默认全局单例，可注入便于测试） */
  dispatcher?: CommandDispatcher
}

/**
 * shell.overlay 锚点：命令触发（openOn=on_command:<commandId>）呈现模态。
 * 无触发零渲染。
 */
export function SlotOverlayHost({ dispatcher = commandDispatcher }: SlotOverlayHostProps): React.ReactElement | null {
  const [entry, setEntry] = useState<SlotEntry | null>(null)

  useEffect(() => {
    return dispatcher.onModalOpen((e) => setEntry(e as SlotEntry))
  }, [dispatcher])

  if (!entry) return null

  return (
    <div
      className="bg-[var(--overlay-bg)] fixed inset-0 z-[200] flex items-center justify-center"
      onClick={() => setEntry(null)}
      role="dialog"
      aria-modal="true"
      data-testid="slot-overlay"
    >
      <div
        className="bg-popover text-popover-foreground w-full max-w-2xl rounded-lg border shadow-xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="border-border flex items-center justify-between border-b px-4 py-3">
          <h2 className="text-base font-medium">{entry.title ?? entry.id}</h2>
          <button
            className="text-muted-foreground hover:text-foreground px-2 text-lg"
            onClick={() => setEntry(null)}
            aria-label="关闭"
            data-testid="slot-overlay-close"
          >
            ×
          </button>
        </div>
        <div className="max-h-[70vh] overflow-y-auto p-4">
          <SlotEntryContent entry={entry} />
        </div>
      </div>
    </div>
  )
}
