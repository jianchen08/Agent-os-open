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
import { useMemo, useRef, type FC, type ReactNode } from 'react'

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
 * BUG-FIX-fix_20260507_markdown_streaming_freeze:
 * 问题根因: @lobehub/ui 的 Markdown 组件在 enableStream 从 true 切换到 false 时
 *          内部流式状态未正确重置，导致组件停止响应后续内容更新。
 *          具体场景：工具调用完成后，旧文本片段的 isLast 变为 false，
 *          enableStream 随之变为 false，Markdown 组件冻结。
 * 修复方案: 当 isStreaming 从 true 变为 false 时，通过改变 key 强制重建组件，
 *          丢弃残留的流式内部状态。同时始终以非流式模式渲染已完成的内容。
 */
export const LobeChatMarkdown: FC<LobeChatMarkdownProps> = ({
  content,
  isStreaming = false,
  onDoubleClick,
  children,
}) => {
  const wasStreamingRef = useRef(false)
  const streamEndedKeyRef = useRef(0)

  if (isStreaming) {
    wasStreamingRef.current = true
  } else if (wasStreamingRef.current) {
    wasStreamingRef.current = false
    streamEndedKeyRef.current += 1
  }

  const markdownKey = useMemo(() => {
    return isStreaming ? 'streaming' : `static-${streamEndedKeyRef.current}`
  }, [isStreaming])

  return (
    <ConfigProvider motion={motion}>
      <div className="lobe-chat-isolated" onDoubleClick={onDoubleClick}>
        {children ?? (
          <Markdown
            key={markdownKey}
            variant="chat"
            enableStream={isStreaming}
            streamSmoothingPreset="balanced"
          >
            {content}
          </Markdown>
        )}
      </div>
    </ConfigProvider>
  )
}
