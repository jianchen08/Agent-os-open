/**
 * Markdown 渲染共享模块
 *
 * SVG 代码块预处理（XSS 过滤 + data URI 内联）与渲染器 memo 比较函数
 */

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
 * 将 ```svg 代码块预处理为 img data URI，使 Markdown 渲染器直接显示 SVG 图形。
 *
 * 使用 Base64 编码避免特殊字符（如括号）破坏 markdown 图片链接语法。
 * mermaid 代码块不做预处理，由 @lobehub/ui Markdown 的 enableMermaid 内置渲染。
 */
export function preprocessSvgCodeBlocks(content: string): string {
  if (!content) return content

  return content.replace(/```svg\s*\n([\s\S]*?)```/gi, (_, svgCode: string) => {
    const sanitized = sanitizeSvg(svgCode.trim())
    const encoded = typeof window !== 'undefined'
      ? window.btoa(unescape(encodeURIComponent(sanitized)))
      : Buffer.from(sanitized, 'utf-8').toString('base64')
    return `![svg](data:image/svg+xml;base64,${encoded})`
  })
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
