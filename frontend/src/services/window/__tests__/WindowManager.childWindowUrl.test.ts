/**
 * buildChildWindowUrl 测试：Electron 子窗口深链 URL 形态
 *
 * 覆盖两类源：
 * - http(s)（dev Vite server / Web 部署）：hash 形态 '/#/p/<pageId>'（现状契约）
 * - app:（BUG-9 修复后的打包件自定义协议源）：pathname 形态 '/p/<pageId>'
 *   （createBrowserRouter 按 pathname 匹配，hash 形态在 app:// 下会落到 HOME）
 */

import { describe, expect, it } from 'vitest'
import { buildChildWindowUrl } from '@/services/window/WindowManager'

describe('buildChildWindowUrl', () => {
  it('http 源（dev Vite）保持 hash 深链形态', () => {
    expect(buildChildWindowUrl('http://localhost:5188', 'http:', 'my-page')).toBe(
      'http://localhost:5188/#/p/my-page',
    )
  })

  it('https 源（Web 部署）保持 hash 深链形态', () => {
    expect(buildChildWindowUrl('https://agentos.example.com', 'https:', 'kb_panel')).toBe(
      'https://agentos.example.com/#/p/kb_panel',
    )
  })

  it('app: 源（打包件自定义协议）用 pathname 深链形态', () => {
    expect(buildChildWindowUrl('app://bundle', 'app:', 'my-page')).toBe('app://bundle/p/my-page')
  })

  it('性质：app: 形态永不含 "#"；http(s) 形态必含 "/#/p/"', () => {
    const pageIds = ['my-page', 'p/with/slash'.replaceAll('/', '-'), 'x']
    for (const id of pageIds) {
      const appUrl = buildChildWindowUrl('app://bundle', 'app:', id)
      expect(appUrl).not.toContain('#')
      expect(appUrl.startsWith('app://bundle/p/')).toBe(true)

      const httpUrl = buildChildWindowUrl('http://localhost:5188', 'http:', id)
      expect(httpUrl).toContain('/#/p/')
    }
  })
})
