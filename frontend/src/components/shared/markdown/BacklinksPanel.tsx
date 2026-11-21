/**
 * 反向链接侧栏（BacklinksPanel）
 *
 * 数据面：docs/graph/backlinks.json 投影经 backlinksProjection 共享模块读取
 * （BacklinksPanel 与 doc_id 点击反查同一数据源）。状态机：loading → list /
 * empty / error；error 与 empty 两态可辨（索引不可用 ≠ 真无反链，禁止静默
 * 降级冒充空态）。
 *
 * 定位语义：filePath（仓库相对路径）→ path_to_id → by_target；投影未含当前文件
 * = 无反链（空态），投影不可读/损坏 = 错误态。
 */

import { useCallback, useEffect, useState, type FC } from 'react'
import { ChevronDown, ChevronRight, Link } from '@/assets/icons'
import { cn } from '@/lib/utils'
import { reportError } from '@/services/errorReporting'
import { openFile } from '@/services/fileOpener'
import { fetchBacklinksProjection, type BacklinkEntry, type BacklinksProjection } from './backlinksProjection'

/** 侧栏状态机 */
type PanelState =
  | { phase: 'loading' }
  | { phase: 'error'; message: string }
  | { phase: 'ready'; projection: BacklinksProjection }

/** 侧栏组件属性 */
export interface BacklinksPanelProps {
  /** 当前文档仓库相对路径（反链定位键） */
  filePath: string
  /** 自定义类名 */
  className?: string
}

/**
 * 反向链接侧栏：列引用当前文档的条目（来源名/关系/说明），点击条目打开来源文档。
 * 折叠态默认展开；来源文档缺失（索引过期）时 openFile 失败经 reportError 通知
 * 中心如实上报（openFile 不自行通知——fileOpener 契约，审查 MF-3 修复面）。
 */
export const BacklinksPanel: FC<BacklinksPanelProps> = ({ filePath, className }) => {
  const [state, setState] = useState<PanelState>({ phase: 'loading' })
  const [collapsed, setCollapsed] = useState(false)

  useEffect(() => {
    let cancelled = false
    setState({ phase: 'loading' })
    void (async () => {
      try {
        const projection = await fetchBacklinksProjection()
        if (!cancelled) setState({ phase: 'ready', projection })
      } catch (err) {
        if (!cancelled) {
          setState({ phase: 'error', message: err instanceof Error ? err.message : '索引读取失败' })
        }
      }
    })()
    return () => {
      cancelled = true
    }
  }, [])

  const entries: BacklinkEntry[] = (() => {
    if (state.phase !== 'ready') return []
    const nodeId = state.projection.path_to_id[filePath]
    if (!nodeId) return []
    return state.projection.by_target[nodeId] ?? []
  })()

  /** 条目点击 → 打开来源文档（仓库相对路径，openFile 自带 _local 回退）；
   *  打开失败经 reportError 上报（审查 MF-3 修复面——失败不得静默） */
  const handleEntryClick = useCallback((entry: BacklinkEntry) => {
    if (!entry.path) return
    void (async () => {
      const result = await openFile(entry.path)
      if (!result.success) {
        reportError(result.message || '打开反链来源失败', {
          component: 'BacklinksPanel',
          action: 'handleEntryClick',
          source: 'frontend',
        })
      }
    })()
  }, [])

  return (
    <aside
      className={cn('border-border bg-muted/20 flex h-full flex-col border-l', className)}
      data-testid="backlinks-panel"
    >
      <button
        type="button"
        onClick={() => setCollapsed((prev) => !prev)}
        aria-expanded={!collapsed}
        className={cn(
          'border-border bg-muted/30 text-foreground flex items-center gap-1.5 border-b px-3 py-2 text-xs font-medium',
          'hover:bg-muted w-full transition-colors',
        )}
      >
        {collapsed ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
        <Link className="h-3.5 w-3.5" />
        <span>反向链接</span>
        {state.phase === 'ready' && <span className="text-muted-foreground">({entries.length})</span>}
      </button>

      {!collapsed && (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {state.phase === 'loading' && (
            <p className="text-muted-foreground px-3 py-2 text-xs">索引加载中…</p>
          )}
          {state.phase === 'error' && (
            <p role="alert" className="text-status-error px-3 py-2 text-xs">
              反向链接索引不可用：{state.message}
            </p>
          )}
          {state.phase === 'ready' && entries.length === 0 && (
            <p className="text-muted-foreground px-3 py-2 text-xs">无反向链接</p>
          )}
          {state.phase === 'ready' && entries.length > 0 && (
            <ul className="divide-border/60 divide-y">
              {entries.map((entry, index) => (
                <li key={`${entry.s}-${entry.k}-${index}`}>
                  <button
                    type="button"
                    onClick={() => handleEntryClick(entry)}
                    className="hover:bg-muted text-foreground block w-full px-3 py-2 text-left text-xs transition-colors"
                    title={entry.path}
                  >
                    <span className="block truncate font-medium">{entry.label || entry.s}</span>
                    <span className="text-muted-foreground mt-0.5 flex items-center gap-1 text-[11px]">
                      <span className="text-primary">{entry.k}</span>
                      {entry.note && <span className="truncate">{entry.note}</span>}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </aside>
  )
}

export default BacklinksPanel
