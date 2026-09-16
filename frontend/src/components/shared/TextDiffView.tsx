/**
 * TextDiffView - 文本差异对比视图组件
 *
 * 展示两个文本版本之间的差异，支持增/删/改行高亮。
 * 底层使用 ReviewDiff 的 computeDiff 算法。
 */

import { buildUnifiedLines, computeDiff } from '@/utils/diffLcs'
import { useMemo } from 'react'
import type { DiffLineType } from '@/types/review'

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
  added: 'bg-status-success/10 text-status-success dark:bg-status-success/20',
  removed: 'bg-status-error/10 text-status-error dark:bg-status-error/20',
}

/** 行前缀标记 */
const LINE_PREFIX: Record<DiffLineType, string> = {
  unchanged: ' ',
  added: '+',
  removed: '-',
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
  const unifiedLines = useMemo(() => buildUnifiedLines(oldLines, newLines), [oldLines, newLines])

  return (
    <div className="text-diff-view flex h-full flex-col" data-testid="text-diff-view">
      {/* 统计栏 */}
      <div className="flex items-center gap-3 border-b border-border px-3 py-2 text-xs text-muted-foreground">
        <span>差异对比</span>
        <span className="text-status-success" data-testid="diff-added-count">+{stats.added}</span>
        <span className="text-status-error" data-testid="diff-removed-count">-{stats.removed}</span>
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
