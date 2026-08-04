/** 消息项组件 显示单条消息，支持用户消息和 AI 消息的不同样式 */

import { Bell, Bot, Check, FileCode, FileText, FileIcon as FileGeneric, Loader2, MessageSquare, Sparkles, User } from 'lucide-react'
import { memo, useEffect, useRef, useState } from 'react'
import { ImageGallery } from '@/components/media/ImageGallery'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { ErrorType, reportError } from '@/services/errorReporting'
import { openAttachment } from '@/services/attachmentOpener'
import { useAgentStore } from '@/stores/agentStore'
import { useInteractionStore } from '@/stores/interactionStore'
import { useSessionStore } from '@/stores/sessionStore'
import { formatTimestamp } from '@/utils/format'
import { safeParseResult } from '@/utils/toolCardRegistry'
import useMessageRender from './hooks/useMessageRender'
import { MessageActions } from './MessageActions'
import MessageContentRenderer from './MessageContentRenderer'
import type { MessageItemProps } from './types'

/** 消息编辑组件 */
interface MessageEditorProps {
  content: string
  onSave: (newContent: string) => void
  onCancel: () => void
  disabled?: boolean
}

const MessageEditor = ({ content, onSave, onCancel, disabled = false }: MessageEditorProps) => {
  const [value, setValue] = useState(content)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  useEffect(() => {
    if (textareaRef.current) {
      textareaRef.current.focus()
      textareaRef.current.setSelectionRange(
        textareaRef.current.value.length,
        textareaRef.current.value.length,
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
    <form onSubmit={handleSubmit} className="flex w-full flex-col gap-2">
      <textarea
        ref={textareaRef}
        value={value}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        disabled={disabled}
        className="border-input bg-background focus:ring-ring min-h-[100px] w-full resize-none rounded-md border p-3 text-sm focus:ring-2 focus:outline-none"
        placeholder="编辑消息内容..."
      />
      <div className="flex items-center justify-between">
        <span className="text-muted-foreground text-xs">Ctrl+Enter 保存，Esc 取消</span>
        <div className="flex gap-2">
          <Button type="button" variant="ghost" size="sm" onClick={onCancel} disabled={disabled}>
            取消
          </Button>
          <Button type="submit" size="sm" disabled={disabled || !value.trim()}>
            {disabled ? (
              <>
                <Loader2 className="mr-1 h-4 w-4 animate-spin" />
                保存中...
              </>
            ) : (
              <>
                <Check className="mr-1 h-4 w-4" />
                保存
              </>
            )}
          </Button>
        </div>
      </div>
    </form>
  )
}

/** 消息项组件 */
export const MessageItem = memo(function MessageItem({
  message,
  isLast = false,
  isGenerating = false,
  onEdit,
  modelName,
  className = '',
  searchQuery,
  taskId,
}: MessageItemProps) {
  const [isEditing, setIsEditing] = useState(false)
  const [versionContent, setVersionContent] = useState<string | null>(null)

  const isUser = message.role === 'user'
  const isAssistant = message.role === 'assistant'
  const isTool = message.role === 'tool'

  const isSystemMessage = message.role === 'system'

  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const isMessageStreaming = message.status === 'streaming'

  const agents = useAgentStore((s) => s.agents)
  const agent = message.agentId ? agents.find((a) => a.id === message.agentId) : null

  const hasPendingInteraction = useInteractionStore(
    (s) =>
      s.pendingInteractions.some(
        (i) =>
          (i.threadId === message.sessionId || i.threadId === activeSessionId) &&
          i.status === 'pending',
      ),
  )

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
    taskId,
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
        className={cn('group hover:bg-muted/30 flex gap-3 px-4 py-2 transition-colors', className)}
        data-testid="message-item"
        data-role="tool"
      >
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-sm">
            <span className="text-muted-foreground font-medium">{toolName}</span>
            <span
              className={cn(
                'rounded-full px-2 py-0.5 text-xs',
                toolStatus === 'completed'
                  ? 'bg-status-success/15 text-status-success'
                  : toolStatus === 'failed'
                    ? 'bg-status-error/15 text-status-error'
                    : toolStatus === 'running'
                      ? 'bg-status-info/15 text-status-info'
                      : 'bg-status-pending/15 text-status-pending',
              )}
            >
              {toolStatus === 'completed'
                ? '已完成'
                : toolStatus === 'failed'
                  ? '失败'
                  : toolStatus === 'running'
                    ? '执行中'
                    : toolStatus}
            </span>
            {durationMs && <span className="text-muted-foreground text-xs">{durationMs}ms</span>}
          </div>
          {toolError && (
            <div className="mt-1 text-sm text-status-error">{toolError}</div>
          )}
          {toolResult && (
            <div className="text-muted-foreground mt-1 text-sm">
              {(() => {
                const parsed = safeParseResult(toolResult)
                if (parsed) {
                  const output = parsed.output as Record<string, unknown> | undefined
                  const taskId = (output?.task_id as string) || (parsed.task_id as string) || ''
                  const status = (output?.status as string) || (parsed.status as string) || ''
                  const message = (output?.message as string) || (parsed.message as string) || ''
                  const parts: string[] = []
                  if (taskId) parts.push(`任务ID: ${taskId}`)
                  if (status) parts.push(`状态: ${status}`)
                  if (message) parts.push(message)
                  return parts.length > 0 ? (
                    <div className="space-y-0.5">{parts.map((p, i) => <div key={i} className={i > 0 ? 'truncate' : ''}>{p}</div>)}</div>
                  ) : (
                    <pre className="truncate whitespace-pre-wrap">{JSON.stringify(parsed, null, 2)}</pre>
                  )
                }
                return (
                  <span className="truncate">
                    {typeof toolResult === 'string' ? toolResult : JSON.stringify(toolResult)}
                  </span>
                )
              })()}
            </div>
          )}
        </div>
      </div>
    )
  }

  return (
    <div
      className={cn(
        'group flex gap-3 px-4 py-3 transition-colors',
        isUser ? 'flex-row-reverse' : '',
        'hover:bg-muted/30',
        className,
      )}
      data-testid="message-item"
      data-role={message.role}
    >
      <Avatar
        className={cn(
          'h-8 w-8 flex-shrink-0 rounded-xl shadow-sm',
          isUser
            ? 'bg-primary text-primary-foreground'
            : isSystemMessage
              ? 'bg-status-warning/15 text-status-warning'
              : 'bg-secondary text-secondary-foreground',
        )}
      >
        <AvatarFallback className="rounded-xl text-sm font-medium">
          {isUser ? (
            <User className="h-4 w-4" />
          ) : isSystemMessage ? (
            <Bell className="h-4 w-4" />
          ) : (
            <Bot className="h-4 w-4" />
          )}
        </AvatarFallback>
      </Avatar>

      <div
        className={cn(
          'flex min-w-0 flex-col',
          isUser ? 'items-end' : 'items-start',
          isUser ? 'max-w-[80%] sm:max-w-[75%]' : 'max-w-[calc(100%-44px)] flex-1',
        )}
      >
        {isEditing ? (
          <div
            className="w-full max-w-full p-3"
            style={{
              background: isUser ? 'var(--bubble-user-bg)' : 'var(--bubble-ai-bg)',
              color: isUser ? 'var(--bubble-user-text)' : 'var(--bubble-ai-text)',
              borderRadius: isUser
                ? 'var(--bubble-user-radius, 1.5rem)'
                : 'var(--bubble-ai-radius, 1rem)',
              boxShadow: isUser
                ? 'var(--bubble-user-shadow, 0 1px 2px 0 rgb(0 0 0 / 0.05))'
                : 'var(--bubble-ai-shadow, 0 1px 2px 0 rgb(0 0 0 / 0.05))',
              border: isUser ? 'var(--bubble-user-border, none)' : 'var(--bubble-ai-border, none)',
              padding: isUser
                ? 'var(--bubble-user-padding, 0.75rem 1rem)'
                : 'var(--bubble-ai-padding, 0.75rem 1rem)',
            }}
          >
            <MessageEditor
              content={message.content}
              onSave={handleSaveEdit}
              onCancel={handleCancelEdit}
              disabled={false}
            />
          </div>
        ) : (
          <>
            {isAssistant && modelName && (
              <div className="text-muted-foreground mb-1 px-1 text-xs">{modelName}</div>
            )}
            {/* 空内容消息跳过气泡渲染 */}
            {(() => {
              const bubbleStyle = {
                // 用 background 而非 backgroundColor：bubble-*-bg 可能是纯色，
                // 也可能是 linear-gradient()（如 ocean-breeze/deep-space 用户气泡）。
                // background-color 遇到渐变值会忽略整条声明 → 背景透明 → 白字看不见。
                background: isUser ? 'var(--bubble-user-bg)' : 'var(--bubble-ai-bg)',
                color: isUser ? 'var(--bubble-user-text)' : 'var(--bubble-ai-text)',
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
              }
              const bubbleCls = cn(
                'overflow-hidden',
                isSystemMessage
                  ? 'w-full border-l-4 border-status-warning/40'
                  : isUser
                    ? 'max-w-full'
                    : 'w-full',
              )

              if (isUser) {
                const userContent = renderContext.displayContent || message.content
                const userAttachments = message.attachments || []
                // 兼容两种字段命名：前端 Attachment.type 和后端持久化的 mime_type
                const getAttMime = (att: { type?: string; mime_type?: string }) =>
                  att.type || att.mime_type || ''
                const imageAttachments = userAttachments
                  .filter((att) => getAttMime(att).startsWith('image/'))
                  .map((att, idx) => ({
                    id: att.id || `img-${idx}`,
                    url: att.url,
                    title: att.name || '图片',
                  }))
                // 非图片附件（文本/文档/代码）：显示文件名 + 类型图标
                const fileAttachments = userAttachments.filter(
                  (att) => !getAttMime(att).startsWith('image/'),
                )
                if (!userContent && imageAttachments.length === 0 && fileAttachments.length === 0) {
                  return null
                }
                return (
                  <div className={bubbleCls} style={bubbleStyle}>
                    {userContent && (
                      <div className="whitespace-pre-wrap break-words text-sm">{userContent}</div>
                    )}
                    {imageAttachments.length > 0 && (
                      <div className="mt-2">
                        <ImageGallery images={imageAttachments} columns={2} />
                      </div>
                    )}
                    {fileAttachments.length > 0 && (
                      <div className="mt-2 flex flex-col gap-1">
                        {fileAttachments.map((att, idx) => {
                          const mime = getAttMime(att)
                          const isCode =
                            mime.startsWith('text/x-') ||
                            mime === 'application/json' ||
                            mime === 'application/javascript' ||
                            mime === 'application/x-yaml'
                          const isTextLike =
                            mime.startsWith('text/') ||
                            mime === 'application/pdf' ||
                            mime === 'application/msword' ||
                            mime.startsWith('application/vnd.')
                          // 代码→FileCode，文档/文本→FileText，其他→FileGeneric
                          const Icon = isCode ? FileCode : isTextLike ? FileText : FileGeneric
                          return (
                            <button
                              type="button"
                              key={att.id || `file-${idx}`}
                              onClick={() => {
                                if (att.url) {
                                  void openAttachment({
                                    id: att.id,
                                    name: att.name || '文件',
                                    url: att.url,
                                  })
                                }
                              }}
                              className="bg-background/60 hover:bg-background flex w-full items-center gap-2 rounded-lg border border-border/30 px-2 py-1.5 text-left text-sm transition-colors"
                            >
                              <Icon className="text-muted-foreground h-4 w-4 shrink-0" />
                              <span className="truncate">{att.name || '文件'}</span>
                            </button>
                          )
                        })}
                      </div>
                    )}
                  </div>
                )
              }

              // 导致消息不渲染。刷新后只有2条消息可见。
              const _rawFallback = renderContext.displayContent || message.content
              const _displayFallback = _rawFallback?.trim() ? _rawFallback : ''

              if (!isMessageStreaming && renderContext.fragments.length === 0 && !_displayFallback) {
                return null
              }

              return (
                <div className={bubbleCls} style={bubbleStyle}>
                  {renderContext.fragments.length === 0 ? (
                    isMessageStreaming ? (
                      <div className="flex items-center gap-2">
                        {hasPendingInteraction ? (
                          <>
                            <MessageSquare className="h-4 w-4 text-status-info" />
                            <span className="text-sm text-status-info">等待用户响应...</span>
                          </>
                        ) : (
                          <>
                            <Loader2 className="h-4 w-4 animate-spin" />
                            <span className="text-sm">思考中...</span>
                          </>
                        )}
                      </div>
                    ) : _displayFallback ? (
                      <div className="whitespace-pre-wrap break-words text-sm">{_displayFallback}</div>
                    ) : null
                  ) : (
                    <MessageContentRenderer
                      fragments={renderContext.fragments}
                      isStreaming={isMessageStreaming}
                      searchQuery={searchQuery}
                    />
                  )}
                </div>
              )
            })()}
          </>
        )}

        <div
          className={cn(
            'text-muted-foreground mt-1.5 flex items-center gap-3 text-xs',
            isUser ? 'flex-row-reverse' : '',
          )}
        >
          {isAssistant && agent && (
            <span className="inline-flex items-center gap-1 rounded-lg bg-[var(--badge-info-bg)] px-2 py-0.5 text-xs text-[var(--badge-info-text)]">
              <Sparkles className="h-3 w-3" />
              <span className="font-medium">{agent.name}</span>
            </span>
          )}

          <span className="text-muted-foreground/70">{formatTimestamp(message.timestamp)}</span>

          {activeSessionId && (
            <div className="opacity-100 md:opacity-0 md:group-hover:opacity-100 transition-opacity duration-200">
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
})
