/*
 * Ported from DeepSeek Harness (DSH) — MIT License.
 * Copyright (c) 2026 DeepSeek. Source: packages/client/ui-primitives/src/ReadBlock.tsx
 * Repo pinned at commit 47f943859bef60e4160492346772ded9b24f765a (version 0.1.0-rc.5).
 * Adapted for AgentOS (task_dsh_plugin_adapter 任务 3):
 * - 剥离 shiki 高亮（markdown/highlight.ts，懒加载语法引擎重依赖）——这是原组件
 *   对未知/缺失语言的合法降级路径（plain monospace），保留 lang 横幅提示；
 *   高亮接入点留在 highlightLines() 单处，后续如引 shiki 只改这一个函数；
 * - 行号 gutter/窗口计数/head-tail 折叠逻辑保持原样。
 */

import { useCallback, useMemo, useState } from 'react'
import clsx from 'clsx'
import { writeClipboard } from './clipboard.ts'
import css from './ReadBlock.module.css'

/**
 * Content lines shown before the height cap collapses the middle. Matches
 * TerminalBlock's default so a long read and a long command output cut at the
 * same place in the same flow.
 */
export const DEFAULT_READ_MAX_LINES = 16

/** One line of the read window: its file line number and its text (no trailing newline). */
export interface ReadBlockLine {
  /** 1-based line number in the file (a window past an offset keeps the file's own numbering). */
  number: number
  /** The line's text, already truncated to the read tool's per-line cap. */
  text: string
}

export interface ReadBlockProps {
  /** Banner label (the file path, or a tool-supplied replacement title); omitted draws no label. */
  label?: string | undefined
  /** The returned window's lines, in file order, each keeping its file line number. */
  lines: readonly ReadBlockLine[]
  /** Exact total line count in the file, for the "showing N of M" note when the read is a window. */
  totalLines: number
  /** Grammar hint (a file-extension-derived language id); shown in the banner. */
  lang?: string | undefined
  /** Height cap in content lines before the middle collapses (default {@link DEFAULT_READ_MAX_LINES}). */
  maxLines?: number | undefined
  /** Extra class merged onto the wrapper (callers position; this component draws). */
  className?: string | undefined
}

/**
 * 高亮接入点：返回 undefined = 全部行渲染纯文本（原组件对未知语言的降级路径）。
 * 后续引入 shiki 时仅替换本函数，组件其余部分零改动。
 */
function highlightLines(
  _raw: string,
  _lang: string | undefined,
): readonly (readonly { text: string }[])[] | undefined {
  return undefined
}

/**
 * Render a read tool result as a line-numbered file view.
 * @param props - see {@link ReadBlockProps}.
 * @returns the read block element.
 */
export function ReadBlock({
  label,
  lines,
  totalLines,
  lang,
  maxLines = DEFAULT_READ_MAX_LINES,
  className,
}: ReadBlockProps) {
  // The raw text the copy control writes: the window's lines joined by newlines,
  // without the file numbers or any chrome.
  const raw = useMemo(() => lines.map(line => line.text).join('\n'), [lines])
  const highlighted = useMemo(() => highlightLines(raw, lang), [raw, lang])
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)

  const onCopy = useCallback(() => {
    if (copied) return
    void writeClipboard(raw).then((ok) => {
      if (!ok) return
      setCopied(true)
      window.setTimeout(() => { setCopied(false) }, 1000)
    })
  }, [copied, raw])

  const onToggle = useCallback(() => { setExpanded(value => !value) }, [])

  const hidden = lines.length - maxLines
  const capped = hidden > 0 && !expanded
  const headLines = Math.ceil(maxLines / 2)
  const tailLines = maxLines - headLines
  // A read is a window when its returned lines are fewer than the file's total.
  const windowed = lines.length < totalLines

  /**
   * Render a slice of the line array as gutter-numbered rows.
   * @param slice - the lines to draw.
   * @returns the row elements.
   */
  const rows = (slice: readonly (readonly [ReadBlockLine, readonly { text: string }[] | undefined])[]) =>
    slice.map(([line, spans]) => (
      <div key={line.number} className={css.line}>
        <span className={css.gutter} aria-hidden>{line.number}</span>
        <span className={css.content}>{spans === undefined ? line.text : spans.map((span, i) => <span key={i}>{span.text}</span>)}</span>
      </div>
    ))

  // Pair each line with its aligned run array up front, so head/tail slicing
  // keeps the two in step without re-indexing.
  const paired = lines.map((line, index): readonly [ReadBlockLine, readonly { text: string }[] | undefined] =>
    [line, highlighted?.[index]])

  return (
    <div className={clsx(css.block, className)} data-read="">
      <div className={css.banner}>
        <div className={css.label}>{label ?? ''}</div>
        <div className={css.action}>
          {windowed && (
            <span className={css.count}>{`显示 ${lines.length} / ${totalLines} 行`}</span>
          )}
          <span className={css.lang}>{lang ?? ''}</span>
          {lines.length > 0 && (
            <button type="button" className={css.copyButton} onClick={onCopy}>
              {copied ? '复制成功' : '复制'}
            </button>
          )}
        </div>
      </div>
      <div className={css.body}>
        {rows(capped ? paired.slice(0, headLines) : paired)}
        {hidden > 0 && (
          <button
            type="button"
            className={css.expand}
            aria-expanded={expanded}
            aria-label={expanded ? '收起内容' : `展开其余 ${hidden} 行`}
            onClick={onToggle}
          >
            {expanded ? '收起' : `… 其余 ${hidden} 行`}
          </button>
        )}
        {capped && rows(paired.slice(paired.length - tailLines))}
      </div>
    </div>
  )
}
