/**
 * @lobehub/ui Markdown 内部子路径的浅类型声明（包内无 .d.mts，exports 通配
 * `./es/*.d.mts` 解析不到类型）。按消费面所需最小声明——content_cards pre 门
 * （ADR 2026-10-14-content-card-registry §3）经此消费默认 CodeBlock 做未命中
 * 回投（memoPre 同参复刻）。
 */
declare module '@lobehub/ui/es/Markdown/components/CodeBlock' {
  import type { ComponentType, ReactNode } from 'react'

  export interface CodeBlockProps {
    children?: ReactNode
    animated?: boolean
    enableHtmlPreview?: boolean
    enableMermaid?: boolean
    fullFeatured?: boolean
    highlight?: Record<string, unknown>
    html?: Record<string, unknown>
    mermaid?: Record<string, unknown>
    [key: string]: unknown
  }

  export const CodeBlock: ComponentType<CodeBlockProps>
}

declare module '@lobehub/ui/es/Markdown/components/MarkdownProvider' {
  import type { ComponentType, ReactNode } from 'react'

  /** MarkdownProvider 上下文（useMarkdownComponents 同参装配的取数面；本仓只消费
   *  pre 门回投装配涉及的键，其余键透传形状按包实现宽松声明） */
  export interface MarkdownContextValue {
    animated?: boolean
    enableHtmlPreview?: boolean
    enableMermaid?: boolean
    fullFeaturedCodeBlock?: boolean
    componentProps?: {
      pre?: Record<string, unknown>
      highlight?: Record<string, unknown>
      html?: Record<string, unknown>
      mermaid?: Record<string, unknown>
      [key: string]: unknown
    }
    [key: string]: unknown
  }

  export function useMarkdownContext(): MarkdownContextValue

  export const MarkdownProvider: ComponentType<{
    children?: ReactNode
    [key: string]: unknown
  }>
}
