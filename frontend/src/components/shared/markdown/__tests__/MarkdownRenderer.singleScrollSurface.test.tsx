/** @ci frontend-test */
/**
 * MarkdownRenderer 消息流竖向单滚动面契约测试
 *
 * 背景（"代码块上滚不动"根因）：streamdown 默认给代码块体/表格体内嵌限高
 * （codeBlockMaxHeight 400 / tableMaxHeight 300）+ overflow-y-auto，长代码块/
 * 表格成为消息流里的嵌套竖向滚动容器——滚轮悬停其上被内层吃掉，页面不动；
 * 流式重解析时内层 scrollTop 重置即窗口内闪烁。
 *
 * 行为契约（用户可观察）：
 * - 代码块体无论长短：无内联 maxHeight、无 overflow-y-auto（无竖向滚动容器，
 *   滚轮恒滚页面），横向 overflow-x-auto 保留；
 * - 表格滚动层：无内联 maxHeight（同上，竖向归页面）。
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { MarkdownRenderer } from '../MarkdownRenderer'

const longCode = Array.from({ length: 60 }, (_, i) => `line_${i} = ${i}`).join('\n')
const longCodeBlock = '```python\n' + longCode + '\n```'
const shortCodeBlock = '```js\nconst a = 1\n```'

const tableMarkdown = (() => {
  const header = '| 列A | 列B |\n| --- | --- |'
  const rows = Array.from({ length: 30 }, (_, i) => `| a${i} | b${i} |`).join('\n')
  return header + '\n' + rows
})()

describe('MarkdownRenderer — 消息流竖向单滚动面', () => {
  it('长代码块体：无内联 maxHeight、无 overflow-y-auto，横向滚动保留', () => {
    const { container } = render(<MarkdownRenderer content={longCodeBlock} />)
    const body = container.querySelector('[data-streamdown="code-block-body"]')
    expect(body).not.toBeNull()
    expect(body!.style.maxHeight).toBe('')
    expect(body!.className).not.toContain('overflow-y-auto')
    expect(body!.className).toContain('overflow-x-auto')
  })

  it('短代码块体：同样无竖向滚动容器（限高关闭与内容长度无关）', () => {
    const { container } = render(<MarkdownRenderer content={shortCodeBlock} />)
    const body = container.querySelector('[data-streamdown="code-block-body"]')
    expect(body).not.toBeNull()
    expect(body!.style.maxHeight).toBe('')
    expect(body!.className).not.toContain('overflow-y-auto')
  })

  it('多行表格滚动层：无内联 maxHeight（竖向归页面）', () => {
    const { container } = render(<MarkdownRenderer content={tableMarkdown} />)
    const wrapper = container.querySelector('[data-streamdown="table-wrapper"]')
    expect(wrapper).not.toBeNull()
    const scrollLayer = wrapper!.lastElementChild as HTMLElement
    expect(scrollLayer.tagName).toBe('DIV')
    expect(scrollLayer.style.maxHeight).toBe('')
  })
})
