/**
 * SVG/Markdown 预处理工具单元测试
 *
 * 验证 sanitizeSvg 和 preprocessSvgCodeBlocks 函数
 */

import { describe, it, expect } from 'vitest'
import { sanitizeSvg, preprocessSvgCodeBlocks } from '../shared'

describe('sanitizeSvg', () => {
  it('保留合法 SVG 元素和属性', () => {
    const input = '<svg><circle cx="50" cy="50" r="40" fill="red"/></svg>'
    const result = sanitizeSvg(input)
    expect(result).toContain('<circle')
    expect(result).toContain('cx="50"')
    expect(result).toContain('fill="red"')
  })

  it('移除 <script> 标签', () => {
    const input = '<svg><script>alert("xss")</script><rect width="100" height="100"/></svg>'
    const result = sanitizeSvg(input)
    expect(result).not.toContain('<script')
    expect(result).not.toContain('alert')
    expect(result).toContain('<rect')
  })

  it('移除 on* 事件处理属性（双引号）', () => {
    const input = '<svg><rect onload="alert(1)" width="100" height="100"/></svg>'
    const result = sanitizeSvg(input)
    expect(result).not.toContain('onload')
    expect(result).toContain('<rect')
  })

  it('移除 on* 事件处理属性（单引号）', () => {
    const input = "<svg><rect onclick='evil()' width='10' height='10'/></svg>"
    const result = sanitizeSvg(input)
    expect(result).not.toContain('onclick')
    expect(result).toContain('width')
  })

  it('移除 on* 事件处理属性（无引号）', () => {
    const input = '<svg><rect onmouseover=alert(1) width="10"/></svg>'
    const result = sanitizeSvg(input)
    expect(result).not.toContain('onmouseover')
    expect(result).toContain('width')
  })

  it('保留文本内容', () => {
    const input = '<svg><text x="10" y="20">Hello SVG</text></svg>'
    const result = sanitizeSvg(input)
    expect(result).toContain('Hello SVG')
  })
})

describe('preprocessSvgCodeBlocks', () => {
  it('将 ```svg 代码块转换为 img data URI（Base64 编码）', () => {
    const input = '```svg\n<svg width="50"><circle r="20"/></svg>\n```'
    const result = preprocessSvgCodeBlocks(input)
    expect(result).not.toContain('```svg')
    expect(result).toContain('![svg]')
    expect(result).toContain('data:image/svg+xml;base64,')
  })

  it('对 SVG 内容做 XSS 过滤后再编码', () => {
    const input = '```svg\n<svg><script>alert(1)</script><rect/></svg>\n```'
    const result = preprocessSvgCodeBlocks(input)
    expect(result).not.toContain('```svg')
    expect(result).toContain('![svg]')
    // script 内容不应出现在结果中
    expect(result).not.toContain('alert')
  })

  it('不影响其他代码块', () => {
    const input = '```javascript\nconst x = 1;\n```'
    const result = preprocessSvgCodeBlocks(input)
    expect(result).toBe(input)
  })

  it('不影响 mermaid 代码块', () => {
    const input = '```mermaid\ngraph TD; A-->B;\n```'
    const result = preprocessSvgCodeBlocks(input)
    expect(result).toBe(input)
  })

  it('不影响没有代码块的纯文本', () => {
    const input = 'Hello world, this is plain text.'
    const result = preprocessSvgCodeBlocks(input)
    expect(result).toBe(input)
  })

  it('处理多个 svg 代码块', () => {
    const input = '```svg\n<svg><rect/>\n```\nText\n```svg\n<svg><circle/>\n```'
    const result = preprocessSvgCodeBlocks(input)
    expect(result).not.toContain('```svg')
    const imgCount = (result.match(/!\[svg\]/g) || []).length
    expect(imgCount).toBe(2)
  })

  it('svg 代码块与普通代码块混合时只转换 svg', () => {
    const input = [
      'Some text',
      '```svg',
      '<svg><rect/>',
      '```',
      'More text',
      '```python',
      'print("hello")',
      '```',
    ].join('\n')
    const result = preprocessSvgCodeBlocks(input)
    expect(result).toContain('![svg]')
    expect(result).toContain('```python')
    expect(result).toContain('print')
  })

  // ———— 审查报告 Should Fix：边界测试 ————

  it('SVG 内容含括号时不破坏 markdown 图片链接（Base64 编码验证）', () => {
    const input = '```svg\n<svg><g transform="translate(10,20)">text</g></svg>\n```'
    const result = preprocessSvgCodeBlocks(input)
    expect(result).not.toContain('```svg')
    expect(result).toContain('![svg]')
    // 结果中不应出现原始括号（Base64 编码后不含明文括号）
    expect(result).not.toContain('translate(10,20)')
    // 结果应该是完整的单行 markdown 图片语法
    expect(result).toMatch(/^!\[svg\]\(data:image\/svg\+xml;base64,[A-Za-z0-9+/=]+\)$/)
  })

  it('SVG 含 <style> 标签时仍能正常编码', () => {
    const input = '```svg\n<svg><style>.cls{fill:red}</style><rect class="cls"/></svg>\n```'
    const result = preprocessSvgCodeBlocks(input)
    expect(result).toContain('![svg]')
    expect(result).toContain('data:image/svg+xml;base64,')
  })

  it('空 SVG 代码块不崩溃', () => {
    const input = '```svg\n```'
    const result = preprocessSvgCodeBlocks(input)
    // 空内容也应生成合法的 data URI，不崩溃
    expect(result).toContain('![svg]')
    expect(result).toContain('data:image/svg+xml;base64,')
  })

  it('大写 ```SVG 代码块标识也能匹配', () => {
    const input = '```SVG\n<svg><rect/>\n```'
    const result = preprocessSvgCodeBlocks(input)
    expect(result).not.toContain('```SVG')
    expect(result).toContain('![svg]')
  })
})
