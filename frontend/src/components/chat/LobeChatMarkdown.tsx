/** LobeChat Markdown 渲染组件 使用 @lobehub/ui 的 Markdown 组件，专为 AI 聊天设计 */

import { ConfigProvider, Markdown } from '@lobehub/ui'
import { motion } from 'motion/react'
import { useMemo, type FC, type ReactNode } from 'react'

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

  return (
    <ConfigProvider motion={motion}>
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
