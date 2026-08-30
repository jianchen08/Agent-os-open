/**
 * codeHighlightStyle — 令牌化语法样式表契约测试
 *
 * 真实缺陷：组件曾内嵌 oneDark 静态调色板（为深底调的色），浅色主题
 * --code-bg 翻亮后字色撞底（字符串绿约 2.0:1），且主题防撞色强制够不到
 * 组件内联样式。契约：本表只准消费 --code-* 主题令牌，一个硬编码色值都不许有。
 */

import { describe, it, expect } from 'vitest'
import { codeHighlightStyle } from '../codeHighlightStyle'

describe('codeHighlightStyle 令牌化样式表', () => {
  it('性质：每个槽位的 color 都消费 --code-* 主题令牌（白名单外即拒）', () => {
    for (const [key, style] of Object.entries(codeHighlightStyle)) {
      const color = (style as { color?: string }).color
      expect(color, `槽位 ${key} 必须声明 color`).toBeTruthy()
      expect(color, `槽位 ${key} 的 color 必须是 --code-* 令牌引用`).toMatch(
        /^var\(--code-(text|comment|keyword|string|number|function|tag|attr)\)$/,
      )
    }
  })

  it('性质：全表序列化中无十六进制 / hsl / rgb 字面色（防回归挂静态调色板）', () => {
    expect(JSON.stringify(codeHighlightStyle)).not.toMatch(/#[0-9a-fA-F]{6}\b|hsl\(|rgba?\(/)
  })

  it('核心语法槽位齐备：关键字/字符串/注释/数字/函数/标签/属性/标点', () => {
    for (const key of ['keyword', 'string', 'comment', 'number', 'function', 'tag', 'attr-name', 'punctuation']) {
      expect(codeHighlightStyle, `缺 ${key} 槽位`).toHaveProperty(key)
    }
  })
})
