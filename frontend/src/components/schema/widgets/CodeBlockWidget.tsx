/**
 * 代码块组件
 *
 * 根据 Schema 渲染带语法高亮的代码块，支持复制按钮、语言标签和行号显示。
 * 使用基于关键字的简单语法高亮，不依赖第三方库。
 *
 * @module CodeBlockWidget
 */

import React, { useState, useMemo, useCallback } from 'react'

/** 语言关键字映射 */
const LANGUAGE_KEYWORDS: Record<string, string[]> = {
  javascript: [
    'const', 'let', 'var', 'function', 'return', 'if', 'else', 'for', 'while',
    'class', 'import', 'export', 'from', 'default', 'new', 'this', 'async',
    'await', 'try', 'catch', 'throw', 'typeof', 'instanceof', 'switch', 'case',
    'break', 'continue', 'true', 'false', 'null', 'undefined', 'void', 'delete',
    'yield', 'of', 'in', 'extends', 'super',
  ],
  typescript: [
    'const', 'let', 'var', 'function', 'return', 'if', 'else', 'for', 'while',
    'class', 'import', 'export', 'from', 'default', 'new', 'this', 'async',
    'await', 'try', 'catch', 'throw', 'typeof', 'instanceof', 'switch', 'case',
    'break', 'continue', 'true', 'false', 'null', 'undefined', 'void', 'delete',
    'interface', 'type', 'enum', 'implements', 'extends', 'as', 'is', 'keyof',
    'readonly', 'abstract', 'declare', 'module', 'namespace', 'public', 'private',
    'protected', 'static', 'super', 'infer', 'never', 'unknown', 'any',
  ],
  python: [
    'def', 'class', 'import', 'from', 'return', 'if', 'elif', 'else', 'for',
    'while', 'try', 'except', 'finally', 'with', 'as', 'lambda', 'yield',
    'pass', 'break', 'continue', 'raise', 'global', 'nonlocal', 'assert',
    'del', 'and', 'or', 'not', 'in', 'is', 'True', 'False', 'None',
    'async', 'await', 'print', 'self',
  ],
  bash: [
    'if', 'then', 'else', 'elif', 'fi', 'for', 'do', 'done', 'while', 'until',
    'case', 'esac', 'function', 'return', 'exit', 'echo', 'export', 'local',
    'readonly', 'set', 'unset', 'shift', 'source', 'true', 'false',
  ],
  sql: [
    'SELECT', 'FROM', 'WHERE', 'INSERT', 'INTO', 'VALUES', 'UPDATE', 'SET',
    'DELETE', 'CREATE', 'TABLE', 'ALTER', 'DROP', 'INDEX', 'JOIN', 'LEFT',
    'RIGHT', 'INNER', 'OUTER', 'ON', 'AND', 'OR', 'NOT', 'NULL', 'IS',
    'IN', 'LIKE', 'BETWEEN', 'ORDER', 'BY', 'GROUP', 'HAVING', 'LIMIT',
    'OFFSET', 'AS', 'DISTINCT', 'COUNT', 'SUM', 'AVG', 'MAX', 'MIN',
    'UNION', 'ALL', 'EXISTS', 'CASE', 'WHEN', 'THEN', 'ELSE', 'END',
    'PRIMARY', 'KEY', 'FOREIGN', 'REFERENCES', 'CONSTRAINT', 'DEFAULT',
  ],
  json: [],
  yaml: [],
  css: [
    'color', 'background', 'margin', 'padding', 'border', 'display', 'position',
    'width', 'height', 'font', 'text', 'flex', 'grid', 'align', 'justify',
    'overflow', 'opacity', 'transform', 'transition', 'animation', 'box-shadow',
    'none', 'auto', 'inherit', 'initial', 'important',
  ],
  html: [],
  go: [
    'func', 'package', 'import', 'return', 'if', 'else', 'for', 'range',
    'switch', 'case', 'default', 'break', 'continue', 'go', 'defer', 'chan',
    'select', 'type', 'struct', 'interface', 'map', 'var', 'const', 'nil',
    'true', 'false', 'make', 'new', 'append', 'len', 'cap', 'err',
  ],
  rust: [
    'fn', 'let', 'mut', 'const', 'if', 'else', 'for', 'while', 'loop',
    'match', 'return', 'struct', 'enum', 'impl', 'trait', 'pub', 'use',
    'mod', 'crate', 'self', 'super', 'where', 'async', 'await', 'move',
    'ref', 'true', 'false', 'None', 'Some', 'Ok', 'Err', 'as', 'in',
  ],
}

/** 语言显示名称映射 */
const LANGUAGE_LABELS: Record<string, string> = {
  javascript: 'JavaScript',
  typescript: 'TypeScript',
  tsx: 'TSX',
  jsx: 'JSX',
  python: 'Python',
  py: 'Python',
  bash: 'Bash',
  shell: 'Shell',
  sh: 'Shell',
  sql: 'SQL',
  json: 'JSON',
  yaml: 'YAML',
  yml: 'YAML',
  css: 'CSS',
  scss: 'SCSS',
  html: 'HTML',
  xml: 'XML',
  go: 'Go',
  rust: 'Rust',
  java: 'Java',
  cpp: 'C++',
  c: 'C',
  markdown: 'Markdown',
  md: 'Markdown',
  plaintext: 'Text',
  text: 'Text',
}

/**
 * 简单语法高亮：基于关键字标注
 *
 * @param code - 源代码文本
 * @param language - 语言标识
 * @returns 高亮标注后的行数组
 */
function highlightCode(
  code: string,
  language: string,
): Array<Array<{ text: string; type: 'plain' | 'keyword' | 'string' | 'comment' | 'number' }>> {
  const lines = code.split('\n')
  const keywords = LANGUAGE_KEYWORDS[language] ?? []
  const keywordSet = new Set(keywords)

  return lines.map((line) => {
    const tokens: Array<{
      text: string
      type: 'plain' | 'keyword' | 'string' | 'comment' | 'number'
    }> = []

    // 注释检测
    const trimmedLine = line.trimStart()
    if (trimmedLine.startsWith('//') || trimmedLine.startsWith('#')) {
      tokens.push({ text: line, type: 'comment' })
      return tokens
    }

    // 使用简单的正则拆分
    const regex =
      /(\/\/.*$|#.*$)|(["'`])(?:(?!\2|\\).|\\.)*\2|(\b\d+(?:\.\d+)?\b)|(\b\w+\b)/g

    let lastIndex = 0
    let match: RegExpExecArray | null

    while ((match = regex.exec(line)) !== null) {
      // 添加匹配之前的普通文本
      if (match.index > lastIndex) {
        tokens.push({ text: line.slice(lastIndex, match.index), type: 'plain' })
      }

      const [fullMatch, comment, _quote, number, word] = match

      if (comment) {
        tokens.push({ text: fullMatch, type: 'comment' })
      } else if (number) {
        tokens.push({ text: fullMatch, type: 'number' })
      } else if (word) {
        if (keywordSet.has(word)) {
          tokens.push({ text: word, type: 'keyword' })
        } else {
          tokens.push({ text: word, type: 'plain' })
        }
      } else {
        // 字符串
        tokens.push({ text: fullMatch, type: 'string' })
      }

      lastIndex = match.index + fullMatch.length
    }

    // 添加剩余文本
    if (lastIndex < line.length) {
      tokens.push({ text: line.slice(lastIndex), type: 'plain' })
    }

    return tokens
  })
}

/** token 颜色类 */
const TOKEN_CLASSES: Record<string, string> = {
  keyword: 'text-status-info font-semibold',
  string: 'text-status-success',
  comment: 'text-muted-foreground italic',
  number: 'text-status-warning',
  plain: '',
}

/**
 * 代码块组件
 *
 * 纯关键字高亮的代码块，支持复制、语言标签和行号。
 *
 * @param props - 组件属性，包含 code、language、showLineNumbers 等
 * @returns 代码块渲染结果
 */
export function CodeBlockWidget(props: Record<string, unknown>) {
  const code = typeof props.code === 'string' ? props.code : ''
  const language = (typeof props.language === 'string' ? props.language : 'text').toLowerCase()
  const showLineNumbers = (props.showLineNumbers as boolean) ?? true
  const title = props.title as string | undefined

  const [copied, setCopied] = useState(false)

  const highlightedLines = useMemo(
    () => highlightCode(code, language),
    [code, language],
  )

  const handleCopy = useCallback(() => {
    navigator.clipboard.writeText(code).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    }).catch(() => {
      // 回退方案
      const textarea = document.createElement('textarea')
      textarea.value = code
      document.body.appendChild(textarea)
      textarea.select()
      document.execCommand('copy')
      document.body.removeChild(textarea)
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }, [code])

  const langLabel = LANGUAGE_LABELS[language] ?? language.toUpperCase()

  return (
    <div className="w-full overflow-hidden rounded-lg border">
      {/* 头部栏 */}
      <div className="flex items-center justify-between border-b bg-muted/30 px-4 py-2">
        <div className="flex items-center gap-2">
          {title ? (
            <span className="text-foreground text-sm font-medium">{title}</span>
          ) : (
            <span className="text-muted-foreground text-xs font-medium uppercase">
              {langLabel}
            </span>
          )}
        </div>
        <button
          onClick={handleCopy}
          className="text-muted-foreground hover:text-foreground flex items-center gap-1 rounded px-2 py-1 text-xs transition-colors hover:bg-muted"
        >
          {copied ? (
            <>
              <svg className="h-3.5 w-3.5 text-status-success" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                <path d="M5 13l4 4L19 7" />
              </svg>
              已复制
            </>
          ) : (
            <>
              <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2}>
                <rect x="9" y="9" width="13" height="13" rx="2" />
                <path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1" />
              </svg>
              复制
            </>
          )}
        </button>
      </div>

      {/* 代码区域 */}
      <div className="bg-zinc-900 overflow-x-auto p-4">
        <pre className="text-sm leading-relaxed">
          <code>
            {highlightedLines.map((tokens, lineIndex) => (
              <div key={lineIndex} className="flex">
                {showLineNumbers && (
                  <span className="mr-4 inline-block w-8 select-none text-right text-status-pending text-xs leading-relaxed">
                    {lineIndex + 1}
                  </span>
                )}
                <span className="flex-1">
                  {tokens.map((token, tokenIndex) => (
                    <span key={tokenIndex} className={TOKEN_CLASSES[token.type]}>
                      {token.text}
                    </span>
                  ))}
                  {tokens.length === 0 && '\n'}
                </span>
              </div>
            ))}
          </code>
        </pre>
      </div>
    </div>
  )
}
