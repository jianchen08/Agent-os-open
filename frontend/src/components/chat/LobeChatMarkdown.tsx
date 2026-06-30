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
import { useMemo, type FC, type ReactNode } from 'react'

import { preprocessSvgCodeBlocks } from './markdown/shared'
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
 * BUG-FIX-fix_20260524_stream_duplicate:
 * 问题根因: @lobehub/ui 的 Markdown 组件在 enableStream={true} 时会在内部再做一次
 *          字符级流式动画（streamSmoothingPreset），与 streamHandler 的 RAF 批处理
 *          （60fps 增量更新）叠加，导致每个字符/词被渲染两次——一次是 RAF 更新的
 *          实际内容，一次是内部平滑动画的重放内容。
 *          症状：流式输出时文本逐字重复（如"房间里房间里"），刷新后正常。
 * 修复方案: 显式传 enableStream={false}（@lobehub/ui 默认值为 true，不传等于开启），
 *          由 RAF 批处理提供流式视觉效果，不使用内部流式动画。
 *          同时移除不再需要的 key/ref/streamSmoothingPreset 逻辑。
 * 影响范围: 流式输出期间的文本渲染
 * 修复日期: 2026-05-24
 */
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
