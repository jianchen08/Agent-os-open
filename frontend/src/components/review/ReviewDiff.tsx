/**
 * ReviewDiff - 版本对比组件
 *
 * 支持左右（side-by-side）和统一（unified）两种 diff 视图。
 * 高亮变更部分：新增绿色、删除红色、未变灰底。
 */

import { Columns2, Rows3 } from 'lucide-react'
import React, { useMemo, useState } from 'react'
import type { DiffLine, DiffLineType } from '@/types/review'

export interface ReviewDiffProps {
  /** 旧版内容 */
  oldContent: string
  /** 新版内容 */
  newContent: string
  /** 显示模式：左右对比 或 统一视图 */
  mode?: 'side-by-side' | 'unified'
}

/** 简易逐行 diff —— 最长公共子序列（LCS）算法 */
function computeDiff(oldText: string, newText: string): { oldLines: DiffLine[]; newLines: DiffLine[] } {
  const oldArr = oldText.split('\n')
  const newArr = newText.split('\n')

  // LCS 动态规划表
  const m = oldArr.length
  const n = newArr.length
  const dp: number[][] = Array.from({ length: m + 1 }, () => Array(n + 1).fill(0))

  for (let i = 1; i <= m; i++) {
    for (let j = 1; j <= n; j++) {
      if (oldArr[i - 1] === newArr[j - 1]) {
        dp[i][j] = dp[i - 1][j - 1] + 1
      } else {
        dp[i][j] = Math.max(dp[i - 1][j], dp[i][j - 1])
      }
    }
  }

  // 回溯生成 diff
  const oldLines: DiffLine[] = []
  const newLines: DiffLine[] = []
  let i = m,
    j = n

  const actions: Array<{ type: DiffLineType; oldIdx?: number; newIdx?: number }> = []

  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldArr[i - 1] === newArr[j - 1]) {
      actions.unshift({ type: 'unchanged', oldIdx: i - 1, newIdx: j - 1 })
      i--
      j--
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      actions.unshift({ type: 'added', newIdx: j - 1 })
      j--
    } else if (i > 0) {
      actions.unshift({ type: 'removed', oldIdx: i - 1 })
      i--
    }
  }

  let oldLineNum = 0
  let newLineNum = 0

  for (const action of actions) {
    if (action.type === 'unchanged') {
      oldLineNum++
      newLineNum++
      oldLines.push({ type: 'unchanged', content: oldArr[action.oldIdx!], lineNumber: oldLineNum })
      newLines.push({ type: 'unchanged', content: newArr[action.newIdx!], lineNumber: newLineNum })
    } else if (action.type === 'removed') {
      oldLineNum++
      oldLines.push({ type: 'removed', content: oldArr[action.oldIdx!], lineNumber: oldLineNum })
    } else if (action.type === 'added') {
      newLineNum++
      newLines.push({ type: 'added', content: newArr[action.newIdx!], lineNumber: newLineNum })
    }
  }

  return { oldLines, newLines }
}

/** 行背景色 */
const lineColor: Record<DiffLineType, string> = {
  unchanged: 'bg-transparent',
  added: 'bg-green-100 text-green-900 dark:bg-green-900/30 dark:text-green-300',
  removed: 'bg-red-100 text-red-900 dark:bg-red-900/30 dark:text-red-300',
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
          <span className="text-green-600">+{stats.added}</span>
          <span className="text-red-600">-{stats.removed}</span>
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
  const unified = useMemo(() => {
    const result: Array<{
      type: DiffLineType
      oldNum?: number
      newNum?: number
      content: string
    }> = []

    let oi = 0
    let ni = 0

    // 交错排列：先输出连续的 removed，再输出连续的 added
    while (oi < oldLines.length || ni < newLines.length) {
      // 输出旧版的 removed 行
      while (oi < oldLines.length && oldLines[oi].type === 'removed') {
        result.push({
          type: 'removed',
          oldNum: oldLines[oi].lineNumber,
          content: oldLines[oi].content,
        })
        oi++
      }
      // 输出新版的 added 行
      while (ni < newLines.length && newLines[ni].type === 'added') {
        result.push({
          type: 'added',
          newNum: newLines[ni].lineNumber,
          content: newLines[ni].content,
        })
        ni++
      }
      // 输出未变更行（两边应该一致）
      if (oi < oldLines.length && oldLines[oi].type === 'unchanged') {
        result.push({
          type: 'unchanged',
          oldNum: oldLines[oi].lineNumber,
          newNum: newLines[ni]?.lineNumber,
          content: oldLines[oi].content,
        })
        oi++
        ni++
      }
    }

    return result
  }, [oldLines, newLines])

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
