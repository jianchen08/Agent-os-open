/**
 * 插件选择弹窗：向 step 的 steps 组合添加插件引用。
 *
 * 数据源为 fetchPipelinePluginCatalog() 的目录（role 分组展示 + 启用状态），
 * 另提供「手动输入引用」——目录覆盖不了的引用形态（公共 step 库 id、
 * "{{...}}" 动态模板）从这里以原始字符串加入。
 */

import { useMemo, useState } from 'react'
import { Plus, Search } from '@/assets/icons'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import type { PipelinePluginCatalogEntry } from '@/services/api/pipelines'

/** role 分组顺序与中文标签（role 缺失归入「未标注」） */
const ROLE_GROUPS: Array<{ key: string | null; label: string }> = [
  { key: 'input', label: '输入 (input)' },
  { key: 'core', label: '核心 (core)' },
  { key: 'output', label: '输出 (output)' },
  { key: null, label: '未标注角色' },
]

/**
 * 插件选择弹窗。
 *
 * @param open 是否打开
 * @param onOpenChange 开关回调
 * @param catalog 插件目录（不可用时传 []，弹窗提示降级）
 * @param excludeIds 已在本 step 组合中的引用（置灰不可重复添加）
 * @param onPick 选中回调（回传将写入 steps 的引用字符串）
 */
export function PluginPickerDialog({
  open,
  onOpenChange,
  catalog,
  excludeIds,
  onPick,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  catalog: PipelinePluginCatalogEntry[]
  excludeIds: string[]
  onPick: (ref: string) => void
}) {
  const [keyword, setKeyword] = useState('')
  const [manualRef, setManualRef] = useState('')

  const exclude = useMemo(() => new Set(excludeIds), [excludeIds])

  const filtered = useMemo(() => {
    const kw = keyword.trim().toLowerCase()
    if (!kw) return catalog
    return catalog.filter(
      (entry) =>
        entry.id.toLowerCase().includes(kw) || entry.name.toLowerCase().includes(kw),
    )
  }, [catalog, keyword])

  const pick = (ref: string) => {
    onPick(ref)
    onOpenChange(false)
    setKeyword('')
    setManualRef('')
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[80vh]">
        <DialogHeader>
          <DialogTitle>添加插件到组合</DialogTitle>
          <DialogDescription>
            选择管道插件加入当前 step；目录未覆盖的引用（step 库 id / 动态模板）用下方手动输入。
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden p-6 pt-2">
          <div className="relative shrink-0">
            <Search className="text-muted-foreground absolute top-1/2 left-2.5 h-3.5 w-3.5 -translate-y-1/2" />
            <Input
              value={keyword}
              onChange={(e) => setKeyword(e.target.value)}
              placeholder="搜索插件 id / 名称…"
              className="h-8 pl-8 text-xs"
              aria-label="搜索插件"
            />
          </div>

          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto pr-1">
            {catalog.length === 0 && (
              <p className="text-muted-foreground py-6 text-center text-xs">
                插件目录不可用（/api/v1/pipelines 获取失败），可使用手动输入引用
              </p>
            )}
            {ROLE_GROUPS.map((group) => {
              const items = filtered.filter((entry) => entry.role === group.key)
              if (items.length === 0) return null
              return (
                <div key={group.key ?? 'none'}>
                  <p className="text-muted-foreground mb-1 text-[10px] font-semibold tracking-wide">
                    {group.label}
                  </p>
                  <div className="space-y-1">
                    {items.map((entry) => {
                      const added = exclude.has(entry.id)
                      return (
                        <button
                          key={entry.id}
                          type="button"
                          disabled={added}
                          onClick={() => pick(entry.id)}
                          className="hover:bg-[var(--hover-overlay)] flex w-full items-center gap-2 rounded-lg border border-transparent px-2 py-1.5 text-left disabled:cursor-not-allowed disabled:opacity-50"
                          aria-label={`添加 ${entry.id}`}
                        >
                          <Plus className="text-muted-foreground h-3.5 w-3.5 shrink-0" />
                          <span className="text-foreground truncate text-xs font-medium">
                            {entry.name}
                          </span>
                          <span className="text-muted-foreground truncate font-mono text-[10px]">
                            {entry.id}
                          </span>
                          <span className="ml-auto flex shrink-0 items-center gap-1.5 text-[10px]">
                            {entry.enabled === false && (
                              <span className="text-status-warning">已禁用</span>
                            )}
                            {entry.enabled === null && (
                              <span className="text-muted-foreground">状态未知</span>
                            )}
                            {added && <span className="text-muted-foreground">已添加</span>}
                          </span>
                        </button>
                      )
                    })}
                  </div>
                </div>
              )
            })}
          </div>

          <div className="flex shrink-0 items-center gap-2 border-t pt-3">
            <Input
              value={manualRef}
              onChange={(e) => setManualRef(e.target.value)}
              placeholder='手动引用，如 "{{state.core_plugin}}" 或 step 库 id'
              className="h-8 font-mono text-xs"
              aria-label="手动输入引用"
              spellCheck={false}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && manualRef.trim()) pick(manualRef.trim())
              }}
            />
            <button
              type="button"
              onClick={() => manualRef.trim() && pick(manualRef.trim())}
              disabled={!manualRef.trim()}
              className="text-foreground hover:bg-[var(--hover-overlay)] rounded-lg border px-3 py-1.5 text-xs disabled:opacity-40"
              style={{ borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))' }}
            >
              添加引用
            </button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  )
}
