/**
 * 消息项组件
 *
 * 显示单条消息，支持用户消息和 AI 消息的不同样式
 * 渲染顺序：thinking -> text -> toolCalls[]
 */

import { cn } from '@/lib/utils'
import { ErrorType, reportError } from '@/services/errorReporting'
import { useAgentStore } from '@/stores/agentStore'
import { useSessionStore } from '@/stores/sessionStore'
import { formatTimestamp } from '@/utils/format'
import {
    Bell,
    Bot,
    Check,
    Loader2,
    Sparkles,
    User,
} from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { MessageActions } from './MessageActions'
import MessageContentRenderer from './MessageContentRenderer'
import useMessageRender from './hooks/useMessageRender'
import type { MessageItemProps } from './types'

/**
 * 消息编辑组件
 */
interface MessageEditorProps {
  content: string
  onSave: (newContent: string) => void
  onCancel: () => void
  disabled?: boolean
}

const MessageEditor = ({
  content,
  onSave,
  onCancel,
  disabled = false,
}: MessageEditorProps) => {
  const [value, setValue] = useState(content)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.focus()
      textareaRef.current.setSelectionRange(
        textareaRef.current.value.length,
        textareaRef.current.value.length
      )
      textareaRef.current.style.height = 'auto'
      textareaRef.current.style.height = `${Math.max(100, textareaRef.current.scrollHeight)}px`
    }
  }, [])

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setValue(e.target.value)
    e.target.style.height = 'auto'
    e.target.style.height = `${Math.max(100, e.target.scrollHeight)}px`
  }

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    if (value.trim() && value !== content) {
      onSave(value)
    } else if (value === content) {
      onCancel()
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      e.preventDefault()
      handleSubmit(e)
    } else if (e.key === 'Escape') {
      onCancel()
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-2 w-full">
      <textarea
        ref={textareaRef}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        disabled={disabled}
        className="w-full min-h-[100px] p-3 rounded-md border border-input bg-background text-sm resize-none focus:outline-none focus:ring-2 focus:ring-ring"
        placeholder="编辑消息内容..."
      />
      <div className="flex items-center justify-between">
        <span className="text-xs text-muted-foreground">
          Ctrl+Enter 保存，Esc 取消
        </span>
        <div className="flex gap-2">
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={onCancel}
            disabled={disabled}
          >
            取消
          </Button>
          <Button type="submit" size="sm" disabled={disabled || !value.trim()}>
            {disabled ? (
              <>
                <Loader2 className="w-4 h-4 mr-1 animate-spin" />
                保存中...
              </>
            ) : (
              <>
                <Check className="w-4 h-4 mr-1" />
                保存
              </>
            )}
          </Button>
        </div>
      </div>
    </form>
  )
}

/**
 * 消息项组件
 */
export const MessageItem = ({
  message,
  isLast = false,
  isGenerating = false,
  onRegenerate: _onRegenerate,
  onEdit,
  onDelete: _onDelete,
  modelName,
  className = '',
}: MessageItemProps) => {
  const [isEditing, setIsEditing] = useState(false)
  const [_isRetrying, _setIsRetrying] = useState(false)
  const [versionContent, setVersionContent] = useState<string | null>(null)

  const isUser = message.role === 'user'
  const isAssistant = message.role === 'assistant'
  const isTool = message.role === 'tool'

  const isSystemMessage = message.metadata?.record_type === 'system' ||
                          message.metadata?.type === 'system' ||
                          message.metadata?.sender_type === 'system'

  const { activeSessionId } = useSessionStore()
  const isMessageStreaming = message.status === 'streaming'

  const { agents } = useAgentStore()
  const agent = message.agentId
    ? agents.find(a => a.id === message.agentId)
    : null

  const handleContentUpdate = (newContent: string) => {
    setVersionContent(newContent)
  }

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(versionContent ?? message.content)
    } catch (err) {
      reportError(err as string, {
        type: ErrorType.CLIENT,
        componentName: 'MessageItem',
        operation: 'copyToClipboard',
        messageId: message.id,
      })
    }
  }

  const handleEdit = () => {
    if (!isEditing) {
      setIsEditing(true)
    }
  }

  const handleSaveEdit = async (newContent: string) => {
    if (onEdit) {
      setIsEditing(false)
      await onEdit(message.id, newContent)
    }
  }

  const handleCancelEdit = () => {
    setIsEditing(false)
  }

  const renderContext = useMessageRender({
    message,
    isLast,
    isGenerating,
    versionContent,
  })

  /** 工具消息独立渲染 */
  if (isTool) {
    const toolName = message.toolName || message.metadata?.name || '工具'
    const toolStatus = message.status || 'completed'
    const toolResult = message.toolResult || message.metadata?.result || message.metadata?.output
    const toolError = message.toolError || message.metadata?.error
    const durationMs = message.durationMs || message.metadata?.duration_ms

    return (
      <div
        className={cn(
          'flex gap-3 px-4 py-2 group transition-colors hover:bg-muted/30',
          className
        )}
        data-testid="message-item"
        data-role="tool"
      >
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 text-sm">
            <span className="font-medium text-muted-foreground">{toolName}</span>
            <span className={cn(
              'px-2 py-0.5 rounded-full text-xs',
              toolStatus === 'completed' ? 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400' :
              toolStatus === 'failed' ? 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400' :
              toolStatus === 'running' ? 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400' :
              'bg-gray-100 text-gray-700 dark:bg-gray-900/30 dark:text-gray-400'
            )}>
              {toolStatus === 'completed' ? '已完成' :
               toolStatus === 'failed' ? '失败' :
               toolStatus === 'running' ? '执行中' : toolStatus}
            </span>
            {durationMs && (
              <span className="text-xs text-muted-foreground">{durationMs}ms</span>
            )}
          </div>
          {toolError && (
            <div className="mt-1 text-sm text-red-600 dark:text-red-400">
              {toolError}
            </div>
          )}
          {toolResult && (
            <div className="mt-1 text-sm text-muted-foreground truncate">
              {typeof toolResult === 'string' ? toolResult : JSON.stringify(toolResult)}
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div
      className={cn(
        'flex gap-3 px-4 py-3 group transition-colors',
        isUser ? 'flex-row-reverse' : '',
        'hover:bg-muted/30',
        className
      )}
      data-testid="message-item"
      data-role={message.role}
    >
      <Avatar
        className={cn(
          'flex-shrink-0 w-8 h-8 rounded-xl shadow-sm',
          isUser
            ? 'bg-primary text-primary-foreground'
            : isSystemMessage
              ? 'bg-amber-100 dark:bg-amber-900/30 text-amber-600 dark:text-amber-400'
              : 'bg-secondary text-secondary-foreground'
        )}
      >
        <AvatarFallback className="rounded-xl text-sm font-medium">
          {isUser ? (
            <User className="w-4 h-4" />
          ) : isSystemMessage ? (
            <Bell className="w-4 h-4" />
          ) : (
            <Bot className="w-4 h-4" />
          )}
        </AvatarFallback>
      </Avatar>

      <div
        className={cn(
          'flex flex-col min-w-0',
          isUser ? 'items-end' : 'items-start',
          isUser ? 'max-w-[80%] sm:max-w-[75%]' : 'flex-1 max-w-[calc(100%-44px)]'
        )}
      >
        {isEditing ? (
          <div
            className="p-3 w-full max-w-full"
            style={{
              backgroundColor: isUser
                ? 'var(--bubble-user-bg)'
                : 'var(--bubble-ai-bg)',
              color: isUser
                ? 'var(--bubble-user-text)'
                : 'var(--bubble-ai-text)',
              borderRadius: isUser
                ? 'var(--bubble-user-radius, 1.5rem)'
                : 'var(--bubble-ai-radius, 1rem)',
              boxShadow: isUser
                ? 'var(--bubble-user-shadow, 0 1px 2px 0 rgb(0 0 0 / 0.05))'
                : 'var(--bubble-ai-shadow, 0 1px 2px 0 rgb(0 0 0 / 0.05))',
              border: isUser
                ? 'var(--bubble-user-border, none)'
                : 'var(--bubble-ai-border, none)',
              padding: isUser
                ? 'var(--bubble-user-padding, 0.75rem 1rem)'
                : 'var(--bubble-ai-padding, 0.75rem 1rem)',
            }}
          >
            <MessageEditor
              content={message.content}
              onSave={handleSaveEdit}
              onCancel={handleCancelEdit}
              disabled={_isRetrying}
            />
          </div>
        ) : (
          <>
            {isAssistant && modelName && (
              <div className="text-xs text-muted-foreground mb-1 px-1">
                {modelName}
              </div>
            )}
            <div
              className={cn(
              'overflow-hidden',
              isSystemMessage
                ? 'w-full border-l-4 border-amber-400'
                : isUser
                  ? 'max-w-full'
                  : 'w-full'
            )}
              style={{
                backgroundColor: isUser
                  ? 'var(--bubble-user-bg)'
                  : 'var(--bubble-ai-bg)',
                color: isUser
                  ? 'var(--bubble-user-text)'
                  : 'var(--bubble-ai-text)',
                borderRadius: isSystemMessage
                  ? 'var(--bubble-ai-radius, 1rem)'
                  : isUser
                    ? 'var(--bubble-user-radius, 1.5rem)'
                    : 'var(--bubble-ai-radius, 1rem)',
                boxShadow: isSystemMessage
                  ? 'var(--bubble-ai-shadow, 0 1px 2px 0 rgb(0 0 0 / 0.05))'
                  : isUser
                    ? 'var(--bubble-user-shadow, 0 1px 2px 0 rgb(0 0 0 / 0.05))'
                    : 'var(--bubble-ai-shadow, 0 1px 2px 0 rgb(0 0 0 / 0.05))',
                border: isSystemMessage
                  ? 'none'
                  : isUser
                    ? 'var(--bubble-user-border, none)'
                    : 'var(--bubble-ai-border, none)',
                padding: isSystemMessage
                  ? 'var(--bubble-ai-padding, 0.75rem 1rem)'
                  : isUser
                    ? 'var(--bubble-user-padding, 0.625rem 1rem)'
                    : 'var(--bubble-ai-padding, 0.75rem 1rem)',
              }}
            >
              <MessageContentRenderer
                fragments={renderContext.fragments}
                isStreaming={isMessageStreaming}
              />
            </div>
          </>
        )}

        <div
          className={cn(
            'flex items-center gap-3 mt-1.5 text-xs text-muted-foreground',
            isUser ? 'flex-row-reverse' : ''
          )}
        >
          {isAssistant && agent && (
            <span className="inline-flex items-center gap-1 px-2 py-0.5 rounded-lg bg-blue-50 text-blue-700 dark:bg-blue-900/20 dark:text-blue-400 text-xs">
              <Sparkles className="w-3 h-3" />
              <span className="font-medium">{agent.name}</span>
            </span>
          )}

          <span className="text-muted-foreground/70">
            {formatTimestamp(message.timestamp)}
          </span>

          {activeSessionId && (
            <div className="opacity-0 group-hover:opacity-100 transition-opacity duration-200">
              <MessageActions
                message={message}
                sessionId={message.sessionId || activeSessionId}
                isUserMessage={isUser}
                isLastMessage={isLast}
                disabled={isGenerating}
                onCopy={handleCopy}
                onEdit={handleEdit}
                onContentUpdate={handleContentUpdate}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  )
}
