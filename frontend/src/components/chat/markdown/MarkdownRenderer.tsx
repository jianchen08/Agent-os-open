/**
 * Markdown 渲染组件
 *
 * 使用 Streamdown 实现 Markdown 渲染
 * Streamdown 本身支持流式渲染和不完整 Markdown 自动补全
 *
 * 注意：需要安装依赖 streamdown
 */

import { memo, type FC } from 'react'
import { Streamdown } from 'streamdown'
import { cn } from '@/lib/utils'
import { markdownMemoComparator } from './shared'

export interface MarkdownRendererProps {
  /** Markdown 内容 */
  content: string
  /** 是否正在流式输出 */
  isStreaming?: boolean
  /** 自定义类名 */
  className?: string
}

/**
 * Markdown 渲染组件
 *
 * 统一渲染器：流式和非流式使用完全相同的渲染逻辑
 */
export const MarkdownRenderer: FC<MarkdownRendererProps> = memo(
  ({ content, isStreaming = false, className }) => {
    return (
      <div className={cn('markdown-content', className)}>
        <Streamdown mode="static" parseIncompleteMarkdown={true}>
          {content}
        </Streamdown>
        {isStreaming && <span className="md-cursor" />}
      </div>
    )
  },
  markdownMemoComparator,
)

MarkdownRenderer.displayName = 'MarkdownRenderer'
