import type { CSSProperties } from 'react'

/**
 * 代码块语法高亮样式表（react-syntax-highlighter Prism 裸键约定）
 *
 * 键为高亮 token 类名裸键，渲染器按类名幂集排列查表合并（与 oneDark 同约定）；
 * `linenumber` 键承接行号配色。颜色一律消费主题令牌 --code-*——themeService
 * 按代码块底色深浅分发两套调色板，高亮字色与底色的撞色防线收口在主题层，
 * 本表禁止出现任何硬编码色值（codeHighlightStyle 测试锁定）。
 */
export const codeHighlightStyle: Record<string, CSSProperties> = {
  comment: { color: 'var(--code-comment)', fontStyle: 'italic' },
  prolog: { color: 'var(--code-comment)' },
  cdata: { color: 'var(--code-comment)' },
  doctype: { color: 'var(--code-text)' },
  punctuation: { color: 'var(--code-text)' },
  entity: { color: 'var(--code-text)', cursor: 'help' },
  linenumber: { color: 'var(--code-comment)', fontStyle: 'italic' },
  'attr-name': { color: 'var(--code-attr)' },
  inserted: { color: 'var(--code-string)' },
  boolean: { color: 'var(--code-number)' },
  constant: { color: 'var(--code-number)' },
  number: { color: 'var(--code-number)' },
  atrule: { color: 'var(--code-number)' },
  keyword: { color: 'var(--code-keyword)' },
  property: { color: 'var(--code-tag)' },
  tag: { color: 'var(--code-tag)' },
  symbol: { color: 'var(--code-tag)' },
  deleted: { color: 'var(--code-tag)' },
  important: { color: 'var(--code-tag)' },
  selector: { color: 'var(--code-string)' },
  string: { color: 'var(--code-string)' },
  char: { color: 'var(--code-string)' },
  builtin: { color: 'var(--code-string)' },
  regex: { color: 'var(--code-string)' },
  'attr-value': { color: 'var(--code-string)' },
  variable: { color: 'var(--code-function)' },
  operator: { color: 'var(--code-function)' },
  function: { color: 'var(--code-function)' },
  url: { color: 'var(--code-function)' },
}
