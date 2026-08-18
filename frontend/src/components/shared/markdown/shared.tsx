/**
 * Markdown 渲染共享模块
 *
 * 提供共享的组件配置、预处理函数和工具函数
 * 双阶段渲染策略：
 * - Phase A（流式阶段）：轻量 Markdown 渲染，不做代码高亮
 * - Phase B（完成阶段）：完整 Markdown + 代码高亮
 */

import { cn } from '@/lib/utils'
import { CodeBlock } from './CodeBlock'
import { MermaidDiagram } from './MermaidDiagram'
import type { Components } from 'react-markdown'

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
 * 预处理 Markdown 内容
 * 处理各种 LLM 输出的数学公式格式
 */
export function preprocessMarkdownContent(content: string): string {
  if (!content) return content

  const processed = content
    // 处理行内数学公式 \( ... \) -> $ ... $
    .replace(/\\{1,2}\(([^\n]*?)\\{1,2}\)/g, (_, c) => `$${c.trim()}$`)
    // 处理块级数学公式 \[ ... \] -> $$ ... $$
    .replace(/\\{1,2}\[([\s\S]*?)\\{1,2}\]/g, (_, c) => `$$${c.trim()}$$`)

  return processed
}

/**
 * 修复流式输出时不完整的 Markdown 结构
 *
 * 自动补全未闭合的结构，避免渲染错乱
 */
export function fixIncompleteMarkdown(content: string, isStreaming: boolean): string {
  if (!isStreaming || !content) {
    return content
  }

  let text = content

  // 代码围栏奇数则补齐
  const fenceCount = (text.match(/```/g) || []).length
  if (fenceCount % 2 === 1) {
    text += '\n```'
  }

  // 行内反引号奇数则补齐
  let inlineTickCount = 0
  for (let i = 0; i < text.length; i++) {
    if (text.startsWith('```', i)) {
      i += 2
      continue
    }
    if (text[i] === '`') {
      inlineTickCount++
    }
  }
  if (inlineTickCount % 2 === 1) {
    text += '`'
  }

  // 确保内容以换行结尾
  if (!text.endsWith('\n')) {
    text += '\n'
  }

  return text
}

/**
 * 创建 Markdown 组件配置
 */
export function createMarkdownComponents(isStreaming: boolean = false): Components {
  return {
    code({ className, children, ...props }) {
      const match = /language-(\w+)/.exec(className || '')
      const language = match ? match[1] : ''
      const codeString = String(children).replace(/\n$/, '')
      const isCodeBlock = match || codeString.includes('\n')

      if (!isCodeBlock) {
        return (
          <code className="md-inline-code" {...props}>
            {children}
          </code>
        )
      }

      if (language === 'mermaid') {
        return <MermaidDiagram code={codeString} />
      }

      return <CodeBlock code={codeString} language={language} isStreaming={isStreaming} />
    },

    pre({ children }) {
      return <>{children}</>
    },

    h1: ({ className, ...props }) => <h1 className={cn('md-h1', className)} {...props} />,
    h2: ({ className, ...props }) => <h2 className={cn('md-h2', className)} {...props} />,
    h3: ({ className, ...props }) => <h3 className={cn('md-h3', className)} {...props} />,
    h4: ({ className, ...props }) => <h4 className={cn('md-h4', className)} {...props} />,

    p: ({ className, ...props }) => <p className={cn('md-p', className)} {...props} />,

    a: ({ className, ...props }) => (
      <a
        className={cn('md-link', className)}
        target="_blank"
        rel="noopener noreferrer"
        {...props}
      />
    ),

    blockquote: ({ className, ...props }) => (
      <blockquote className={cn('md-blockquote', className)} {...props} />
    ),

    ul: ({ className, ...props }) => <ul className={cn('md-ul', className)} {...props} />,

    ol: ({ className, ...props }) => <ol className={cn('md-ol', className)} {...props} />,

    li: ({ className, ...props }) => <li className={cn('md-li', className)} {...props} />,

    hr: ({ className, ...props }) => <hr className={cn('md-hr', className)} {...props} />,

    table: ({ className, ...props }) => (
      <div className="md-table-wrapper">
        <table className={cn('md-table', className)} {...props} />
      </div>
    ),
    thead: ({ className, ...props }) => <thead className={cn('md-thead', className)} {...props} />,
    tbody: ({ className, ...props }) => <tbody className={cn('md-tbody', className)} {...props} />,
    tr: ({ className, ...props }) => <tr className={cn('md-tr', className)} {...props} />,
    th: ({ className, ...props }) => <th className={cn('md-th', className)} {...props} />,
    td: ({ className, ...props }) => <td className={cn('md-td', className)} {...props} />,

    strong: ({ className, ...props }) => (
      <strong className={cn('md-strong', className)} {...props} />
    ),
    em: ({ className, ...props }) => <em className={cn('md-em', className)} {...props} />,
    del: ({ className, ...props }) => <del className={cn('md-del', className)} {...props} />,

    img: ({ className, alt, ...props }) => (
      <img className={cn('md-img', className)} alt={alt} loading="lazy" {...props} />
    ),
  }
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
