/** LobeChat Markdown 渲染组件 使用 @lobehub/ui 的 Markdown 组件，专为 AI 聊天设计 */

import { ConfigProvider, Markdown } from '@lobehub/ui'
import { motion } from 'motion/react'
import { useMemo, type FC, type ReactNode } from 'react'

import { preprocessSvgCodeBlocks } from '@/components/shared/markdown/shared'
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

  // 深色主题适配说明（@lobehub/ui 5.32.2）：
  // Markdown 内部的 Shiki 高亮用内置 "lobe-theme"，其颜色全部引用 antd-style 的
  // --ant-color-* CSS 变量（如 --ant-color-bg-container），随作用域内变量值自适应明暗。
  // 本项目刻意不挂 lobehub 的 ThemeProvider（避免注入全局 antd 主题/样式），
  // 明暗跟随由 LobeChatMarkdown.css 中 .lobe-chat-isolated 的 token 桥接实现——
  // 把 lobehub 用到的 --ant-color-* 重映射到项目主题变量（深色时 --foreground 为亮色）。
  // 注意：ConfigProvider 在该版本只负责 i18n/CDN/motion，类型与实现均无 appearance
  // 通道（旧版的 appearance prop 已废弃，传入会被静默丢弃），不要再往上传主题明暗。

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
