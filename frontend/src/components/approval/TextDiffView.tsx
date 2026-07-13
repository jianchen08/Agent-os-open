/**
 * TextDiffView - 文本差异对比视图组件
 *
 * 展示两个文本版本之间的差异，支持增/删/改行高亮。
 * 底层使用 ReviewDiff 的 computeDiff 算法。
 */

import React, { useMemo } from 'react'
import type { DiffLine, DiffLineType } from '@/types/review'

export interface TextDiffViewProps {
  /** 旧版文本内容 */
  oldContent: string
  /** 新版文本内容 */
  newContent: string
  /** 是否显示行号，默认 true */
  showLineNumbers?: boolean
}

/** 行背景色 class */
const LINE_COLOR: Record<DiffLineType, string> = {
  unchanged: 'bg-transparent',
  added: 'bg-green-100 text-green-900 dark:bg-green-900/30 dark:text-green-300',
  removed: 'bg-red-100 text-red-900 dark:bg-red-900/30 dark:text-red-300',
}

/** 行前缀标记 */
const LINE_PREFIX: Record<DiffLineType, string> = {
  unchanged: ' ',
  added: '+',
  removed: '-',
}

/**
 * 简易逐行 diff —— 最长公共子序列（LCS）算法
 */
function computeDiff(oldText: string, newText: string): { oldLines: DiffLine[]; newLines: DiffLine[] } {
  const oldArr = oldText.split('\n')
  const newArr = newText.split('\n')

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

  const oldLines: DiffLine[] = []
  const newLines: DiffLine[] = []
  let i = m
  let j = n

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

/**
 * TextDiffView
 *
 * 展示文本差异对比视图，高亮新增、删除、未变更行。
 */
export function TextDiffView({
  oldContent,
  newContent,
  showLineNumbers = true,
}: TextDiffViewProps) {
  const { oldLines, newLines } = useMemo(
    () => computeDiff(oldContent, newContent),
    [oldContent, newContent],
  )

  /** 统计变更 */
  const stats = useMemo(() => {
    const added = newLines.filter((l) => l.type === 'added').length
    const removed = oldLines.filter((l) => l.type === 'removed').length
    const unchanged = newLines.filter((l) => l.type === 'unchanged').length
    return { added, removed, unchanged }
  }, [oldLines, newLines])

  /** 合并两个列表为统一视图 */
  const unifiedLines = useMemo(() => {
    const result: Array<{
      type: DiffLineType
      oldNum?: number
      newNum?: number
      content: string
    }> = []

    let oi = 0
    let ni = 0

    while (oi < oldLines.length || ni < newLines.length) {
      while (oi < oldLines.length && oldLines[oi].type === 'removed') {
        result.push({
          type: 'removed',
          oldNum: oldLines[oi].lineNumber,
          content: oldLines[oi].content,
        })
        oi++
      }
      while (ni < newLines.length && newLines[ni].type === 'added') {
        result.push({
          type: 'added',
          newNum: newLines[ni].lineNumber,
          content: newLines[ni].content,
        })
        ni++
      }
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
    <div className="text-diff-view flex h-full flex-col" data-testid="text-diff-view">
      {/* 统计栏 */}
      <div className="flex items-center gap-3 border-b border-border px-3 py-2 text-xs text-muted-foreground">
        <span>差异对比</span>
        <span className="text-green-600" data-testid="diff-added-count">+{stats.added}</span>
        <span className="text-red-600" data-testid="diff-removed-count">-{stats.removed}</span>
        <span data-testid="diff-unchanged-count">~{stats.unchanged}</span>
      </div>

      {/* 统一 diff 视图 */}
      <div className="flex-1 overflow-auto">
        <div className="font-mono text-xs" data-testid="diff-content">
          {unifiedLines.map((line, idx) => (
            <div
              key={idx}
              className={`flex ${LINE_COLOR[line.type]}`}
              data-testid={`diff-line-${idx}`}
              data-line-type={line.type}
            >
              {showLineNumbers && (
                <>
                  <span className="w-8 shrink-0 select-none border-r border-border px-1 text-right text-muted-foreground/50">
                    {line.oldNum ?? ''}
                  </span>
                  <span className="w-8 shrink-0 select-none border-r border-border px-1 text-right text-muted-foreground/50">
                    {line.newNum ?? ''}
                  </span>
                </>
              )}
              <span className="w-5 shrink-0 select-none text-center font-bold">
                {LINE_PREFIX[line.type]}
              </span>
              <span className="flex-1 whitespace-pre-wrap break-all px-1">
                {line.content}
              </span>
            </div>
          ))}
          {unifiedLines.length === 0 && (
            <div className="px-3 py-4 text-center text-xs text-muted-foreground" data-testid="diff-empty">
              两个版本内容相同
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
