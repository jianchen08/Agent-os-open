/** LobeChat Markdown 渲染组件 使用 @lobehub/ui 的 Markdown 组件，专为 AI 聊天设计 */

import { ConfigProvider, Markdown } from '@lobehub/ui'
import { motion } from 'motion/react'
import { useMemo, type FC, type ReactNode } from 'react'

import { useThemeStore } from '@/stores/themeStore'
import { preprocessSvgCodeBlocks } from './markdown/shared'
import './LobeChatMarkdown.css'

interface LobeChatMarkdownProps {
  content: string
  isStreaming?: boolean
  onDoubleClick?: () => void
  children?: ReactNode
}

/** LobeChat Markdown 渲染组件 */
export const LobeChatMarkdown: FC<LobeChatMarkdownProps> = ({
  content,
  isStreaming = false,
  onDoubleClick,
  children,
}) => {
  const processedContent = useMemo(
    () => preprocessSvgCodeBlocks(content),
    [content],
  )

  // lobehub 的 Markdown 内部用 Shiki 高亮代码块，主题（github-light / github-dark）
  // 由 appearance 决定。未传 appearance 时 lobehub 默认 "light"，导致深色主题下
  // 代码块被渲染成白底深字，与深色页面冲突而看不清。这里把项目解析后的明暗同步过去，
  // 让 Shiki 选用与当前主题匹配的深/浅高亮主题。
  const resolvedTheme = useThemeStore((s) => s.resolvedTheme)

  return (
    <ConfigProvider motion={motion} appearance={resolvedTheme}>
      <div className="lobe-chat-isolated" onDoubleClick={onDoubleClick}>
        {children ?? (
          <Markdown variant="chat" enableStream={false} enableMermaid={true}>
            {processedContent}
          </Markdown>
        )}
        {isStreaming && <span className="md-cursor" />}
      </div>
    </ConfigProvider>
  )
}
