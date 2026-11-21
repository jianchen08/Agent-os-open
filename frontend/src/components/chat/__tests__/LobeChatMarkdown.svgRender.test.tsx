/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * LobeChatMarkdown · SVG 代码块渲染（2026-11-10 修复：闭合后整块消失）
 *
 * 缺陷：```svg 围栏流式中以代码块显示（流式门保持源码态），闭合后
 * preprocessSvgCodeBlocks 把它改写成 `![svg](data:image/svg+xml;base64,…)`；
 * 但 @lobehub/ui 未传 react-markdown 的 urlTransform，默认白名单
 * （http(s)/irc/mailto/xmpp）把 data: 清成空 src → AttachmentImage 空 src
 * 返回 null → 整个代码块从消息流消失。
 *
 * 修复：LobeChatMarkdown 经 reactMarkdownProps 下发 shared.markdownUrlTransform，
 * 仅放行 img src 的 base64 图片 data URI，其余协议仍按默认白名单拦截。
 */
import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { LobeChatMarkdown } from '../LobeChatMarkdown'

/** 取首个 img 的 src（无 img 返回 null） */
function firstImgSrc(container: HTMLElement): string | null {
  const img = container.querySelector('img')
  return img ? img.getAttribute('src') : null
}

/** data URI 载荷解码（生成侧 btoa(unescape(encodeURIComponent(x)))，逆运算即还原） */
function decodeDataUri(src: string): string {
  const base64 = src.slice(src.indexOf(',') + 1)
  return decodeURIComponent(escape(window.atob(base64)))
}

describe('LobeChatMarkdown SVG 代码块渲染', () => {
  it('闭合的 ```svg 块渲染为图片（data URI 不被清空）', () => {
    const svg = '<svg width="50" height="50"><circle cx="25" cy="25" r="20" fill="blue"/></svg>'
    const { container } = render(<LobeChatMarkdown content={`\`\`\`svg\n${svg}\n\`\`\``} />)

    const src = firstImgSrc(container)
    expect(src, 'SVG 代码块闭合后应渲染出图片').toBeTruthy()
    expect(src).toMatch(/^data:image\/svg\+xml;base64,/)
    const decoded = decodeDataUri(src as string)
    expect(decoded).toContain('<circle cx="25" cy="25" r="20" fill="blue"/>')
    // 缺 xmlns 的 SVG 作为 img 加载时 Chromium 不渲染（naturalWidth=0），载荷必须带
    expect(decoded).toContain('xmlns="http://www.w3.org/2000/svg"')
  })

  it('流式输出完成后 ```svg 代码块转为图片（不再整块消失）', () => {
    const open = '```svg\n<svg width="10" height="10"><rect width="10" height="10"/></svg>'
    const { container, rerender } = render(<LobeChatMarkdown content={open} isStreaming />)
    expect(firstImgSrc(container), '流式中保持源码态（代码块），不出图').toBeNull()

    rerender(<LobeChatMarkdown content={`${open}\n\`\`\``} />)
    expect(firstImgSrc(container)).toMatch(/^data:image\/svg\+xml;base64,/)
  })

  it('SVG 内的 script 与 on* 事件属性在渲染前被移除', () => {
    const svg =
      '<svg><script>alert(1)</script><rect onload="evil()" width="10" height="10"/></svg>'
    const { container } = render(<LobeChatMarkdown content={`\`\`\`svg\n${svg}\n\`\`\``} />)

    const src = firstImgSrc(container)
    expect(src).toBeTruthy()
    const decoded = decodeDataUri(src as string)
    expect(decoded).not.toContain('<script')
    expect(decoded).not.toMatch(/\sonload=/i)
    expect(decoded).toContain('<rect')
  })

  it('data:image 以外的 data URI 仍被拦截（不渲染 img）', () => {
    const { container } = render(
      <LobeChatMarkdown content={'![x](data:text/html;base64,PHNjcmlwdD4=)'} />,
    )
    expect(firstImgSrc(container)).toBeNull()
  })

  it('javascript: 图片地址仍被拦截（不渲染 img）', () => {
    const { container } = render(
      <LobeChatMarkdown content={'![x](javascript:alert(1))'} />,
    )
    expect(firstImgSrc(container)).toBeNull()
  })

  it('普通 https 图片不受放行策略影响，照常渲染', () => {
    const { container } = render(
      <LobeChatMarkdown content={'![x](https://example.com/pic.png)'} />,
    )
    expect(firstImgSrc(container)).toBe('https://example.com/pic.png')
  })
})
