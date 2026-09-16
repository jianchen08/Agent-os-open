/**
 * 简易逐行 diff —— 最长公共子序列（LCS）算法。
 * ReviewDiff 与 TextDiffView 共用（曾经两处整段复制，jscpd 克隆门禁收口）。
 */
import type { DiffLine, DiffLineType } from '@/types/review'

export function computeDiff(oldText: string, newText: string): { oldLines: DiffLine[]; newLines: DiffLine[] } {
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

/** 交错排列 old/new 行生成统一视图行（先连续 removed 再连续 added，未变更行双游标推进） */
export function buildUnifiedLines(
  oldLines: DiffLine[],
  newLines: DiffLine[],
): Array<{ type: DiffLineType; oldNum?: number; newNum?: number; content: string }> {
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
}
