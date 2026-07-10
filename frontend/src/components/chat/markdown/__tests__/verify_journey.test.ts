/**
 * SVG/Mermaid 渲染功能验证 — 用户旅程调用链验证
 * 
 * 本文件验证 sanitizeSvg 和 preprocessSvgCodeBlocks 在真实输入下的实际输出，
 * 不依赖 mock，直接调用源码函数，验证从输入到输出的完整链路。
 */
import { describe, it, expect } from 'vitest'
import { sanitizeSvg, preprocessSvgCodeBlocks } from '../shared'

describe('用户旅程 — SVG 渲染完整链路', () => {
  it('场景1: SVG 代码块 → Base64 图片 → Base64 解码内容正确', () => {
    const input = '```svg\n<svg width="50" height="50"><circle cx="25" cy="25" r="20" fill="blue"/></svg>\n```'
    const output = preprocessSvgCodeBlocks(input)

    // 预处理后应为图片 markdown 语法
    expect(output).toContain('![svg]')
    expect(output).toContain('data:image/svg+xml;base64,')

    // Base64 解码后内容应包含原始 SVG 元素
    const b64 = output.match(/base64,([A-Za-z0-9+/=]+)\)/)?.[1]
    expect(b64).toBeDefined()
    const decoded = Buffer.from(b64!, 'base64').toString('utf-8')
    expect(decoded).toContain('<circle')
    expect(decoded).toContain('fill="blue"')

    // 输出必须是单行完整 markdown 图片语法
    expect(output).toMatch(/^!\[svg\]\(data:image\/svg\+xml;base64,[A-Za-z0-9+/=]+\)$/)
  })

  it('场景2: Mermaid 代码块原样保留（交给 enableMermaid 渲染）', () => {
    const input = '```mermaid\ngraph TD; A-->B;\n```'
    const output = preprocessSvgCodeBlocks(input)
    expect(output).toBe(input) // 原样返回，不做预处理
    expect(output).toContain('```mermaid')
    expect(output).toContain('graph TD')
  })

  it('场景3: XSS 安全过滤 — script 标签 + onload 事件均移除', () => {
    const malicious = '<svg><script>alert("xss")</script><rect onload="evil()" width="10"/></svg>'
    const cleaned = sanitizeSvg(malicious)

    expect(cleaned.toLowerCase()).not.toContain('<script')
    expect(cleaned).not.toContain('alert')
    expect(cleaned.toLowerCase()).not.toContain('onload')
    expect(cleaned).not.toContain('evil')
    expect(cleaned).toContain('<rect') // 合法元素保留
  })

  it('场景4: 不影响 javascript / python 等普通代码块', () => {
    const input = '```javascript\nconst x = 1;\n```\n\n```python\nprint("hello")\n```'
    const output = preprocessSvgCodeBlocks(input)
    expect(output).toBe(input)
    expect(output).toContain('```javascript')
    expect(output).toContain('```python')
  })

  it('场景5: SVG 含括号的 transform 属性 Base64 编码正确', () => {
    const input = '```svg\n<svg><g transform="translate(10,20)">text</g></svg>\n```'
    const output = preprocessSvgCodeBlocks(input)

    expect(output).toContain('![svg]')
    expect(output).not.toContain('translate(10,20)') // 明文括号不应出现

    // 完整 markdown 图片语法
    expect(output).toMatch(/^!\[svg\]\(data:image\/svg\+xml;base64,[A-Za-z0-9+/=]+\)$/)

    // Base64 解码后括号内容完整保留
    const b64 = output.match(/base64,([A-Za-z0-9+/=]+)\)/)?.[1]
    const decoded = Buffer.from(b64!, 'base64').toString('utf-8')
    expect(decoded).toContain('translate(10,20)')
    expect(decoded).toContain('text')
  })

  it('组合场景: SVG+XSS+Mermaid+TypeScript 混合内容', () => {
    const combo = [
      'Here is a diagram:',
      '',
      '```mermaid',
      'graph LR; A-->B;',
      '```',
      '',
      'And an SVG:',
      '',
      '```svg',
      '<svg><script>bad()</script><rect onload="evil()" width="20"/></svg>',
      '```',
      '',
      '```typescript',
      'const x: number = 42;',
      '```',
    ].join('\n')

    const output = preprocessSvgCodeBlocks(combo)

    // Mermaid 保留
    expect(output).toContain('```mermaid')
    expect(output).toContain('graph LR')
    // SVG 替换为图片
    expect(output).toContain('![svg]')
    expect(output).not.toContain('```svg')
    // XSS 过滤
    expect(output).not.toContain('<script')
    expect(output).not.toContain('evil')
    // TypeScript 保留
    expect(output).toContain('```typescript')
    expect(output).toContain('const x')
  })
})

describe('补充场景 — 边界与异常', () => {
  it('空字符串输入不崩溃', () => {
    expect(preprocessSvgCodeBlocks('')).toBe('')
    expect(sanitizeSvg('')).toBe('')
  })

  it('null/undefined 输入不崩溃', () => {
    expect(preprocessSvgCodeBlocks(null as unknown as string)).toBe(null)
    expect(preprocessSvgCodeBlocks(undefined as unknown as string)).toBe(undefined)
  })

  it('只有 ```svg 开头没有结尾 — 不匹配，原样返回', () => {
    const input = '```svg\n<svg><rect/></svg>'
    const output = preprocessSvgCodeBlocks(input)
    expect(output).toBe(input)
  })

  it('多个 on* 事件属性全部移除', () => {
    const input = '<svg onclick="a()" onmouseover="b()" onerror="c()" onload="d()"><rect/></svg>'
    const output = sanitizeSvg(input)
    expect(output.toLowerCase()).not.toContain('onclick')
    expect(output.toLowerCase()).not.toContain('onmouseover')
    expect(output.toLowerCase()).not.toContain('onerror')
    expect(output.toLowerCase()).not.toContain('onload')
    expect(output).toContain('<rect')
  })

  it('大写 SVG 标识匹配', () => {
    const input = '```SVG\n<svg><rect/></svg>\n```'
    const output = preprocessSvgCodeBlocks(input)
    expect(output).toContain('![svg]')
    expect(output).not.toContain('```SVG')
  })
})
