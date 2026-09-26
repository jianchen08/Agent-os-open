/**
 * ReviewDiff - 版本对比组件
 *
 * 支持左右（side-by-side）和统一（unified）两种 diff 视图。
 * 高亮变更部分：新增绿色、删除红色、未变灰底。
 */

import { useMemo, useState } from 'react'
import { Columns2, Rows3 } from '@/assets/icons'
import { buildUnifiedLines, computeDiff } from '@/utils/diffLcs'
import type { DiffLine, DiffLineType } from '@/types/review'

export interface ReviewDiffProps {
  /** 旧版内容 */
  oldContent: string
  /** 新版内容 */
  newContent: string
  /** 显示模式：左右对比 或 统一视图 */
  mode?: 'side-by-side' | 'unified'
}


/** 行背景色 */
const lineColor: Record<DiffLineType, string> = {
  unchanged: 'bg-transparent',
  added: 'bg-status-success/10 text-status-success dark:bg-status-success/20',
  removed: 'bg-status-error/10 text-status-error dark:bg-status-error/20',
}

/** 行前缀标记 */
const linePrefix: Record<DiffLineType, string> = {
  unchanged: ' ',
  added: '+',
  removed: '-',
}

/**
 * ReviewDiff
 *
 * 展示两个版本的内容对比，支持切换显示模式。
 */
export function ReviewDiff({
  oldContent,
  newContent,
  mode: initialMode = 'side-by-side',
}: ReviewDiffProps) {
  const [mode, setMode] = useState<'side-by-side' | 'unified'>(initialMode)

  const { oldLines, newLines } = useMemo(
    () => computeDiff(oldContent, newContent),
    [oldContent, newContent],
  )

  /** 统计变更 */
  const stats = useMemo(() => {
    const added = newLines.filter((l) => l.type === 'added').length
    const removed = oldLines.filter((l) => l.type === 'removed').length
    return { added, removed }
  }, [oldLines, newLines])

  return (
    <div className="review-diff flex h-full flex-col">
      {/* 工具栏 */}
      <div className="flex items-center gap-2 border-b border-border px-3 py-2">
        <span className="text-sm font-medium text-foreground">版本对比</span>
        <div className="ml-2 flex items-center gap-1 text-xs text-muted-foreground">
          <span className="text-status-success">+{stats.added}</span>
          <span className="text-status-error">-{stats.removed}</span>
        </div>
        <div className="ml-auto flex items-center gap-1 rounded-md border border-border p-0.5">
          <button
            className={`flex items-center gap-1 rounded px-2 py-0.5 text-xs transition-colors ${
              mode === 'side-by-side' ? 'bg-accent text-foreground' : 'text-muted-foreground hover:text-foreground'
            }`}
            onClick={() => setMode('side-by-side')}
            title="左右对比"
          >
            <Columns2 className="h-3 w-3" />
            左右
          </button>
          <button
            className={`flex items-center gap-1 rounded px-2 py-0.5 text-xs transition-colors ${
              mode === 'unified' ? 'bg-accent text-foreground' : 'text-muted-foreground hover:text-foreground'
            }`}
            onClick={() => setMode('unified')}
            title="统一视图"
          >
            <Rows3 className="h-3 w-3" />
            统一
          </button>
        </div>
      </div>

      {/* Diff 内容 */}
      <div className="flex-1 overflow-auto">
        {mode === 'side-by-side' ? (
          <div className="flex">
            {/* 左侧：旧版 */}
            <div className="flex-1 border-r border-border">
              <div className="border-b border-border bg-muted/30 px-3 py-1 text-xs text-muted-foreground">
                旧版本
              </div>
              <DiffLineList lines={oldLines} />
            </div>
            {/* 右侧：新版 */}
            <div className="flex-1">
              <div className="border-b border-border bg-muted/30 px-3 py-1 text-xs text-muted-foreground">
                新版本
              </div>
              <DiffLineList lines={newLines} />
            </div>
          </div>
        ) : (
          <UnifiedDiffView oldLines={oldLines} newLines={newLines} />
        )}
      </div>
    </div>
  )
}

/** Diff 行列表 */
function DiffLineList({ lines }: { lines: DiffLine[] }) {
  return (
    <div className="font-mono text-xs">
      {lines.map((line, idx) => (
        <div
          key={idx}
          className={`flex ${lineColor[line.type]}`}
        >
          <span className="w-8 shrink-0 select-none border-r border-border px-1 text-right text-muted-foreground/50">
            {line.lineNumber}
          </span>
          <span className="w-5 shrink-0 select-none text-center font-bold">{linePrefix[line.type]}</span>
          <span className="flex-1 whitespace-pre-wrap break-all px-1">{line.content}</span>
        </div>
      ))}
      {lines.length === 0 && (
        <div className="px-3 py-4 text-center text-xs text-muted-foreground">无内容</div>
      )}
    </div>
  )
}

/** 统一 diff 视图 —— 合并新旧行按顺序排列 */
function UnifiedDiffView({
  oldLines,
  newLines,
}: {
  oldLines: DiffLine[]
  newLines: DiffLine[]
}) {
  const unified = useMemo(() => buildUnifiedLines(oldLines, newLines), [oldLines, newLines])

  return (
    <div className="font-mono text-xs">
      {unified.map((line, idx) => (
        <div key={idx} className={`flex ${lineColor[line.type]}`}>
          <span className="w-8 shrink-0 select-none border-r border-border px-1 text-right text-muted-foreground/50">
            {line.oldNum ?? ''}
          </span>
          <span className="w-8 shrink-0 select-none border-r border-border px-1 text-right text-muted-foreground/50">
            {line.newNum ?? ''}
          </span>
          <span className="w-5 shrink-0 select-none text-center font-bold">{linePrefix[line.type]}</span>
          <span className="flex-1 whitespace-pre-wrap break-all px-1">{line.content}</span>
        </div>
      ))}
      {unified.length === 0 && (
        <div className="px-3 py-4 text-center text-xs text-muted-foreground">两个版本内容相同</div>
      )}
    </div>
  )
}
