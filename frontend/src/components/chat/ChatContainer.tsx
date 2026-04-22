/**
 * 聊天容器组件
 *
 * 整合消息列表和输入区域的完整聊天界面
 */

import { Loader2 } from 'lucide-react'
import { ChatInput } from './ChatInput'
import { MessageList } from './MessageList'
import type { ChatContainerProps } from './types'

/**
 * 聊天容器组件
 */
export const ChatContainer = ({
  sessionId,
  messages,
  isLoading = false,
  isGenerating = false,
  onSendMessage,
  onStopGenerate,
  onRegenerate,
  onEdit,
  onDelete,
  currentTokenUsage = 0,
  maxTokens = 128000,
  modelName = 'glm-4.7',
  thinkingMode,
  toggleThinkingMode,
  className = '',
  hasMoreMessages = false,
  isLoadingMoreMessages = false,
  onLoadMoreMessages,
}: ChatContainerProps) => {
  /** 加载状态 */
  if (isLoading) {
    return (
      <div
        className={`flex flex-col h-full items-center justify-center ${className}`}
        data-testid="chat-container-loading"
      >
        <Loader2 className="w-8 h-8 animate-spin text-primary" />
        <p className="mt-2 text-muted-foreground">加载中...</p>
      </div>
    )
  }

  return (
    <div
      className={`flex flex-col h-full ${className}`}
      data-testid="chat-container"
      data-session-id={sessionId}
    >
      {/* 消息列表 */}
      <MessageList
        messages={messages}
        isGenerating={isGenerating}
        onRegenerate={onRegenerate}
        onEdit={onEdit}
        onDelete={onDelete}
        modelName={modelName}
        className="flex-1"
        hasMore={hasMoreMessages}
        isLoadingMore={isLoadingMoreMessages}
        onLoadMore={onLoadMoreMessages}
        sessionId={sessionId}
      />

      {/* 输入区域 */}
      <ChatInput
        isGenerating={isGenerating}
        onSendMessage={onSendMessage}
        onStopGenerate={onStopGenerate}
        placeholder="输入消息，按 Enter 发送..."
        enableThinkingMode={true}
        modelName={modelName}
        currentTokenUsage={currentTokenUsage}
        maxTokens={maxTokens}
        thinkingMode={thinkingMode}
        toggleThinkingMode={toggleThinkingMode}
      />
    </div>
  )
}
