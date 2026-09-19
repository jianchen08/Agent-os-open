/**
 * 输入区引用镜像行（通用）——按注册的 reference provider 渲染各域实时选中：
 * 选中出现 / 取消消失（订阅驱动），行尾 × 主动清除当前引用。
 * 数据、清除动作与线程激活全经 provider 注册缝，本组件不感知具体插件域
 * （godot 等域自带 provider 即可获得镜像行，ADR 2026-09-10 展示面扩展）。
 */
import { useEffect, useMemo, useState } from 'react'
import { X } from '@/assets/icons'
import { getReferenceProviders, type ReferenceRowState } from '@/services/references'
import { cn } from '@/lib/utils'
import { ReferenceChip } from './ReferenceChip'

export function ReferenceSelectionRow({ threadId }: { threadId?: string }) {
  // provider 集合在模块装载期定型（注册即副作用），挂载期快照足够
  const providers = useMemo(() => getReferenceProviders().filter((p) => p.getRow), [])

  const [, setTick] = useState(0)

  useEffect(() => {
    const unsubs = providers.map((p) => {
      void p.activateRow?.(threadId)
      return p.subscribeRow?.(() => setTick((n) => n + 1))
    })
    return () => {
      unsubs.forEach((unsub) => unsub?.())
    }
  }, [providers, threadId])

  const rows = providers
    .map((p) => p.getRow?.(threadId) ?? null)
    .filter((row): row is ReferenceRowState => row !== null)
  if (rows.length === 0) return null

  return (
    <>
      {rows.map((row) => (
        <div
          key={row.label}
          className="mb-2 flex flex-wrap items-center gap-2"
          data-testid="reference-selection-row"
        >
          <span className="text-muted-foreground inline-flex items-center gap-1.5 text-[11px]">
            <span
              className={`inline-block h-1.5 w-1.5 rounded-full ${
                row.connected ? 'bg-status-success' : 'bg-status-warning'
              }`}
            />
            {row.label}
          </span>
          {row.chips.map((chip) => (
            <ReferenceChip
              key={chip.key}
              data={{
                kind: chip.kind,
                title: chip.title,
                subtitle: chip.subtitle,
                previewUrl: chip.previewUrl,
              }}
            />
          ))}
          <button
            type="button"
            onClick={() => {
              void row.clear()
            }}
            className={cn(
              'flex h-icon-md w-icon-md items-center justify-center rounded',
              'hover:bg-destructive/20 text-muted-foreground hover:text-destructive',
            )}
            title={`清除 ${row.label}`}
            aria-label={`清除 ${row.label}`}
            data-testid="reference-selection-clear"
          >
            <X className="h-icon-xs w-icon-xs" />
          </button>
        </div>
      ))}
    </>
  )
}
