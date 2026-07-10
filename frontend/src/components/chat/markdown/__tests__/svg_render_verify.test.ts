/**
 * SVG/Mermaid 渲染功能验证脚本（可独立运行）
 *
 * 运行方式:
 *   cd frontend
 *   node node_modules/.bin/vitest run --pool=threads \
 *     src/components/chat/markdown/__tests__/svg_render_verify.test.ts
 *
 * 覆盖验证场景:
 *   1. SVG 代码块 → Base64 图片转换
 *   2. Mermaid 代码块不受预处理影响
 *   3. XSS 安全过滤（script + on* 事件）
 *   4. 普通代码块不受影响
 *   5. SVG 含括号的 transform 属性
 *   + 组合场景、边界异常场景
 */
import { describe, it, expect } from 'vitest'
import { sanitizeSvg, preprocessSvgCodeBlocks } from '../shared'

describe('SVG 代码块渲染用户旅程', () => {
  it('场景1: SVG 代码块 → Base64 图片 → 解码内容正确', () => {
    const input = '```svg\n<svg width="50" height="50"><circle cx="25" cy="25" r="20" fill="blue"/></svg>\n```'
    const output = preprocessSvgCodeBlocks(input)

    expect(output).toContain('![svg]')
    expect(output).toContain('data:image/svg+xml;base64,')

    const b64 = output.match(/base64,([A-Za-z0-9+/=]+)\)/)?.[1]
    expect(b64).toBeDefined()
    const decoded = Buffer.from(b64!, 'base64').toString('utf-8')
    expect(decoded).toContain('<circle')
    expect(decoded).toContain('fill="blue"')
    expect(output).toMatch(/^!\[svg\]\(data:image\/svg\+xml;base64,[A-Za-z0-9+/=]+\)$/)
  })

  it('场景2: Mermaid 代码块原样保留', () => {
    const input = '```mermaid\ngraph TD; A-->B;\n```'
    const output = preprocessSvgCodeBlocks(input)
    expect(output).toBe(input)
    expect(output).toContain('```mermaid')
  })

  it('场景3: XSS 安全过滤', () => {
    const malicious = '<svg><script>alert("xss")</script><rect onload="evil()" width="10"/></svg>'
    const cleaned = sanitizeSvg(malicious)
    expect(cleaned.toLowerCase()).not.toContain('<script')
    expect(cleaned).not.toContain('alert')
    expect(cleaned.toLowerCase()).not.toContain('onload')
    expect(cleaned).not.toContain('evil')
    expect(cleaned).toContain('<rect')
  })

  it('场景4: 不影响普通代码块', () => {
    const input = '```javascript\nconst x = 1;\n```\n\n```python\nprint("hello")\n```'
    const output = preprocessSvgCodeBlocks(input)
    expect(output).toBe(input)
    expect(output).toContain('```javascript')
    expect(output).toContain('```python')
  })

  it('场景5: SVG 含括号 transform', () => {
    const input = '```svg\n<svg><g transform="translate(10,20)">text</g></svg>\n```'
    const output = preprocessSvgCodeBlocks(input)
    expect(output).toContain('![svg]')
    expect(output).not.toContain('translate(10,20)')
    const b64 = output.match(/base64,([A-Za-z0-9+/=]+)\)/)?.[1]
    const decoded = Buffer.from(b64!, 'base64').toString('utf-8')
    expect(decoded).toContain('translate(10,20)')
  })

  it('组合场景: SVG+XSS+Mermaid+TypeScript', () => {
    const combo = [
      '```mermaid', 'graph LR; A-->B;', '```', '',
      '```svg', '<svg><script>bad()</script><rect onload="evil()" width="20"/></svg>', '```', '',
      '```typescript', 'const x: number = 42;', '```',
    ].join('\n')
    const output = preprocessSvgCodeBlocks(combo)
    expect(output).toContain('```mermaid')
    expect(output).toContain('![svg]')
    expect(output).not.toContain('<script')
    expect(output).not.toContain('evil')
    expect(output).toContain('```typescript')
  })
})

describe('边界与异常场景', () => {
  it('空字符串不崩溃', () => {
    expect(preprocessSvgCodeBlocks('')).toBe('')
    expect(sanitizeSvg('')).toBe('')
  })

  it('null/undefined 输入不崩溃', () => {
    expect(preprocessSvgCodeBlocks(null as unknown as string)).toBe(null)
  })

  it('未闭合 ```svg 不匹配', () => {
    const input = '```svg\n<svg><rect/></svg>'
    expect(preprocessSvgCodeBlocks(input)).toBe(input)
  })

  it('多个 on* 事件属性全部移除', () => {
    const input = '<svg onclick="a()" onmouseover="b()" onerror="c()"><rect/></svg>'
    const output = sanitizeSvg(input)
    expect(output.toLowerCase()).not.toContain('onclick')
    expect(output.toLowerCase()).not.toContain('onmouseover')
    expect(output.toLowerCase()).not.toContain('onerror')
  })

  it('大写 SVG 标识匹配', () => {
    const input = '```SVG\n<svg><rect/></svg>\n```'
    const output = preprocessSvgCodeBlocks(input)
    expect(output).toContain('![svg]')
    expect(output).not.toContain('```SVG')
  })
})
