/**
 * LobeChat Markdown 渲染组件
 *
 * 使用 @lobehub/ui 的 Markdown 组件，专为 AI 聊天设计
 * 特性：流式渲染、不完整 Markdown 自动补全、代码高亮、表格等
 *
 * 注意：需要安装依赖 @lobehub/ui 和 motion
 */

import { ConfigProvider, Markdown } from '@lobehub/ui'
import { motion } from 'motion/react'
import type { FC, ReactNode } from 'react'

import './LobeChatMarkdown.css'

interface LobeChatMarkdownProps {
  content: string
  isStreaming?: boolean
  onDoubleClick?: () => void
  children?: ReactNode
}

/**
 * LobeChat Markdown 渲染组件
 *
 * 统一渲染器：流式和非流式使用相同的渲染逻辑
 * 流式阶段使用 enableStream 进行增量渲染
 */
export const LobeChatMarkdown: FC<LobeChatMarkdownProps> = ({
  content,
  isStreaming = false,
  onDoubleClick,
  children,
}) => {
  return (
    <ConfigProvider motion={motion}>
      <div className="lobe-chat-isolated" onDoubleClick={onDoubleClick}>
        {children ?? (
          <Markdown variant="chat" enableStream={isStreaming} streamSmoothingPreset="balanced">
            {content}
          </Markdown>
        )}
      </div>
    </ConfigProvider>
  )
}
