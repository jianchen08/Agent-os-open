/**
 * CodeBlock — 语法字色令牌化接线测试
 *
 * 契约：高亮与流式两条渲染链路的字色/底色一律消费 --code-* 主题令牌；
 * 组件内不允许出现静态高亮调色板（oneDark 直引已退役）。
 */

import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { CodeBlock } from '../CodeBlock'

const JS_SAMPLE = 'const answer = "42" // done'

describe('CodeBlock 主题令牌接线', () => {
  it('高亮链路：token 内联样式消费 --code-* 令牌，无字面色值', () => {
    const { container } = render(<CodeBlock code={JS_SAMPLE} language="javascript" />)
    const html = container.innerHTML
    expect(html, '关键字应消费 --code-keyword').toContain('var(--code-keyword)')
    expect(html, '字符串应消费 --code-string').toContain('var(--code-string)')
    expect(html, '基准字色应消费 --code-text').toContain('var(--code-text)')
    expect(html, '不得出现硬编码色值').not.toMatch(/#[0-9a-fA-F]{6}\b|hsl\(/)
  })

  it('流式链路：pre 直接消费 --code-bg / --code-text 令牌', () => {
    const { container } = render(<CodeBlock code={JS_SAMPLE} language="javascript" isStreaming />)
    const pre = container.querySelector('pre')
    const style = pre?.getAttribute('style') ?? ''
    expect(style, '流式底色应消费 --code-bg').toContain('var(--code-bg)')
    expect(style, '流式字色应消费 --code-text').toContain('var(--code-text)')
  })
})
