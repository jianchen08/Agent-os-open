/**
 * 聊天容器组件
 *
 * 整合消息列表和输入区域的完整聊天界面
 */

import { Loader2, Search, X } from 'lucide-react'
import { useState, useMemo } from 'react'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import ErrorBoundary from '@/components/ErrorBoundary'
import { ChatInput } from './ChatInput'
import { InteractionPanel } from './InteractionPanel'
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
  modelName = 'glm-5.1',
  thinkingMode,
  toggleThinkingMode,
  className = '',
  hasMoreMessages = false,
  isLoadingMoreMessages = false,
  onLoadMoreMessages,
}: ChatContainerProps) => {
  /** 搜索状态 */
  const [searchQuery, setSearchQuery] = useState('')

  /**
   * 过滤消息
   */
  const filteredMessages = useMemo(() => {
    if (!searchQuery.trim()) {
      return messages
    }

    const query = searchQuery.toLowerCase()
    return messages.filter((message) => {
      // 搜索消息内容
      if (message.content?.toLowerCase().includes(query)) {
        return true
      }

      // 搜索工具调用名称
      if (message.toolCalls?.some((tool) => tool.tool_name?.toLowerCase().includes(query))) {
        return true
      }

      return false
    })
  }, [messages, searchQuery])

  /** 加载状态 */
  if (isLoading) {
    return (
      <div
        className={`flex h-full flex-col items-center justify-center ${className}`}
        data-testid="chat-container-loading"
      >
        <Loader2 className="text-primary h-8 w-8 animate-spin" />
        <p className="text-muted-foreground mt-2">加载中...</p>
      </div>
    )
  }

  return (
    <div
      className={`flex h-full flex-col ${className}`}
      data-testid="chat-container"
      data-session-id={sessionId}
    >
      {/* 搜索栏 */}
      <div className="bg-background shrink-0 border-b">
        <div className="flex items-center gap-2 px-4 py-2">
          <div className="relative flex-1">
            <Search className="text-muted-foreground absolute top-1/2 left-3 h-4 w-4 -translate-y-1/2 transform" />
            <Input
              type="text"
              placeholder="搜索消息内容..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="h-8 pr-8 pl-9 text-sm"
            />
            {searchQuery && (
              <Button
                variant="ghost"
                size="sm"
                className="absolute top-1/2 right-1 h-6 w-6 -translate-y-1/2 transform p-0"
                onClick={() => setSearchQuery('')}
              >
                <X className="h-3 w-3" />
              </Button>
            )}
          </div>
          {searchQuery && (
            <div className="text-muted-foreground text-sm">
              找到 {filteredMessages.length} 条消息
            </div>
          )}
        </div>
      </div>

      {/* 消息列表 */}
      <MessageList
        messages={filteredMessages}
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
        searchQuery={searchQuery}
      />

      {/* 人类交互卡片 */}
      <ErrorBoundary>
        <InteractionPanel sessionId={sessionId} />
      </ErrorBoundary>

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
