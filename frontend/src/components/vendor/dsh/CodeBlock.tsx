/*
 * Ported from DeepSeek Harness (DSH) — MIT License.
 * Copyright (c) 2026 DeepSeek. Source: packages/client/ui-primitives/src/markdown/CodeBlock.tsx
 * Repo pinned at commit 47f943859bef60e4160492346772ded9b24f765a (version 0.1.0-rc.5).
 * Adapted for AgentOS (task_dsh_plugin_adapter 任务 3):
 * - shiki 高亮 → react-syntax-highlighter Prism（灵汐既有依赖，oneDark 主题，
 *   与 chat/markdown/CodeBlock 同源）——避免为移植引入 shiki 重依赖；
 * - 横幅/复制/几何保持原样（CSS Module 原样复制）。
 */

import { useCallback, useState } from 'react'
import clsx from 'clsx'
import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter'
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism'
import { writeClipboard } from './clipboard.ts'
import css from './CodeBlock.module.css'

export interface CodeBlockProps {
  /** The source text, rendered verbatim (trailing newline trimmed for display). */
  code: string
  /** Grammar hint (markdown fence info string or a fixed caller id); unknown = plain. */
  lang?: string | undefined
  /** Extra class merged onto the wrapper (callers position; this component draws). */
  className?: string | undefined
  /** Copy-button idle label; the owner passes localized copy (this package is cordis-free, so copy arrives via props). */
  copyLabel?: string | undefined
  /** Copy-button label during the post-copy confirmation window. */
  copiedLabel?: string | undefined
}

export function CodeBlock({ code, lang, className, copyLabel = '复制', copiedLabel = '复制成功' }: CodeBlockProps) {
  const trimmed = code.endsWith('\n') ? code.slice(0, -1) : code
  const [copied, setCopied] = useState(false)

  const onCopy = useCallback(() => {
    if (copied) return
    void writeClipboard(trimmed).then((ok) => {
      if (!ok) return
      setCopied(true)
      window.setTimeout(() => { setCopied(false) }, 1000)
    })
  }, [copied, trimmed])

  const body = lang === undefined || lang === ''
    ? (
      <pre className={css.plain}><code>{trimmed}</code></pre>
    )
    : (
      <SyntaxHighlighter
        language={lang}
        style={oneDark}
        customStyle={{
          margin: 0,
          padding: '16px',
          background: 'transparent',
          borderBottomLeftRadius: 'var(--dsl-code-block-border-radius)',
          borderBottomRightRadius: 'var(--dsl-code-block-border-radius)',
          font: 'var(--dsw-font-markdown-code-block)',
        }}
        codeTagProps={{ style: { font: 'inherit', background: 'none' } }}
        wrapLongLines={false}
      >
        {trimmed}
      </SyntaxHighlighter>
    )

  return (
    <div className={clsx(css.block, 'md-code-block', className)}>
      <div className={css.bannerWrap}>
        <div className={css.banner}>
          <div className={css.infostring}>{lang ?? ''}</div>
          <div className={css.action}>
            <button type="button" className={css.copyButton} onClick={onCopy}>
              {copied ? copiedLabel : copyLabel}
            </button>
          </div>
        </div>
      </div>
      {body}
    </div>
  )
}
