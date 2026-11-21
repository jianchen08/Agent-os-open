/**
 * Markdown 渲染共享模块
 *
 * SVG 代码块预处理（XSS 过滤 + data URI 内联）、markdown URL 放行策略与
 * 渲染器 memo 比较函数
 */

import { defaultUrlTransform } from 'react-markdown'

/**
 * SVG XSS 安全过滤：移除 script 标签和 on* 事件属性
 *
 * DEBT: 仅做基础过滤（script + on* 事件），不覆盖 foreignObject / javascript: URI /
 *       iframe / object / embed / style 内 @import 等高级向量。
 *       ceiling: 依赖 <img> 标签沙箱作为安全边界（浏览器不执行 img 内 SVG 脚本）。
 *       upgrade: 若将来改为 inline SVG 渲染（dangerouslySetInnerHTML），必须替换为 DOMPurify。
 */
export function sanitizeSvg(svg: string): string {
  return svg
    .replace(/<script[\s\S]*?<\/script>/gi, '')
    .replace(/\son\w+\s*=\s*"[^"]*"/gi, '')
    .replace(/\son\w+\s*=\s*'[^']*'/gi, '')
    .replace(/\son\w+\s*=\s*[^\s>]+/gi, '')
}

/**
 * 补齐根 <svg> 的 xmlns 命名空间声明。
 *
 * SVG 以 data URI 作为 <img> 加载时按“独立 SVG 文档”解析：根元素缺
 * xmlns="http://www.w3.org/2000/svg" 会被当成无命名空间的 XML，Chromium
 * naturalWidth=0（实测 50×50 的同一图形：无 xmlns 0×0、有 xmlns 50×50），
 * 页面上就是空白。LLM 输出的 ```svg 常省略 xmlns，此处按需补上。
 */
export function ensureSvgXmlns(svg: string): string {
  // 根标签匹配容忍引号内的 ">"（如 aria-label="a > b"），只替换首个 <svg …>
  return svg.replace(/<svg\b((?:"[^"]*"|'[^']*'|[^>"'])*)>/i, (tag, attrs: string) =>
    /\bxmlns\s*=/i.test(attrs) ? tag : `<svg xmlns="http://www.w3.org/2000/svg"${attrs}>`,
  )
}

/**
 * 将 ```svg 代码块预处理为 img data URI，使 Markdown 渲染器直接显示 SVG 图形。
 *
 * 使用 Base64 编码避免特殊字符（如括号）破坏 markdown 图片链接语法。
 * mermaid 代码块不做预处理，由 @lobehub/ui Markdown 的 enableMermaid 内置渲染。
 */
export function preprocessSvgCodeBlocks(content: string): string {
  if (!content) return content

  return content.replace(/```svg\s*\n([\s\S]*?)```/gi, (_, svgCode: string) => {
    const sanitized = ensureSvgXmlns(sanitizeSvg(svgCode.trim()))
    const encoded = typeof window !== 'undefined'
      ? window.btoa(unescape(encodeURIComponent(sanitized)))
      : Buffer.from(sanitized, 'utf-8').toString('base64')
    return `![svg](data:image/svg+xml;base64,${encoded})`
  })
}

/**
 * markdown URL 放行策略（react-markdown `urlTransform` 覆写）。
 *
 * react-markdown 默认只放行 http(s)/irc/mailto/xmpp，其余协议一律清空为 ""——
 * preprocessSvgCodeBlocks 产出的 `![svg](data:image/svg+xml;base64,…)` 因此被清成
 * 空 src，消息流里的 SVG 代码块刚闭合就整块消失（AttachmentImage 空 src 不渲染）。
 * 这里仅对 img src 的 base64 图片 data URI 放行：<img> 沙箱不执行脚本，与本文件
 * preprocessSvgCodeBlocks 的安全边界一致；其余 URL 仍按默认白名单裁决。
 */
export function markdownUrlTransform(url: string, key?: string): string {
  if (key === 'src' && /^data:image\/[a-z0-9.+-]+;base64,/i.test(url)) {
    return url
  }
  return defaultUrlTransform(url)
}

/**
 * Markdown 渲染器 Props 比较函数
 */
export function markdownMemoComparator(
  prev: { content: string; isStreaming?: boolean; className?: string },
  next: { content: string; isStreaming?: boolean; className?: string },
): boolean {
  if (next.isStreaming) {
    return false
  }
  return (
    prev.content === next.content &&
    prev.isStreaming === next.isStreaming &&
    prev.className === next.className
  )
}
