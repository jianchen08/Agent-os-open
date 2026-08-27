/** 消息项组件 显示单条消息，支持用户消息和 AI 消息的不同样式 */

import {
  AlertCircleIcon as AlertCircle,
  Bell,
  Bot,
  Check,
  FileCode,
  FileText,
  FileIcon as FileGeneric,
  Loader2,
  MessageSquare,
  Sparkles,
  User,
} from '@/assets/icons'
import { memo, useEffect, useRef, useState } from 'react'
import ActivityCard from './ActivityCard'
import { ErrorSourceBadge } from '@/components/shared/ErrorSourceBadge'
import { ImageGallery } from '@/components/media/ImageGallery'
import { LobeChatMarkdown } from '@/components/chat/LobeChatMarkdown'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { ErrorType, reportError } from '@/services/errorReporting'
import { openAttachment } from '@/services/attachmentOpener'
import { useInteractionStore } from '@/stores/interactionStore'
import { useAgentsQuery } from '@/hooks/queries/useAgentsQuery'
import { useSessionStore } from '@/stores/sessionStore'
import { useThemeStore } from '@/stores/themeStore'
import { formatTimestamp } from '@/utils/format'
import { toolCallToActivity } from '@/utils/activityConverter'
import { getGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import useMessageRender from './hooks/useMessageRender'
import { MessageActions } from './MessageActions'
import MessageContentRenderer from './MessageContentRenderer'
import { parseReferenceMessage, ReferenceChip } from './ReferenceChip'
import type { MessageItemProps } from './types'
import type { MessageToolCall } from '@/types/models'

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
                <Loader2 className="mr-1 h-icon-md w-icon-md animate-spin" />
                保存中...
              </>
            ) : (
              <>
                <Check className="mr-1 h-icon-md w-icon-md" />
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
  onRegenerate,
  onRollbackTo,
  modelName,
  className = '',
  searchQuery,
  taskId,
}: MessageItemProps) {
  const [isEditing, setIsEditing] = useState(false)
  const [versionContent, setVersionContent] = useState<string | null>(null)

  const isUser = message.role === 'user'
  const isAssistant = message.role === 'assistant'
  // AI 消息气泡形态（主题声明开关：DSH 皮肤主题 flat=平铺跟原生一致；
  // 角色扮演类主题可声明 bubble 恢复沉浸感气泡）
  const bubbleAiMode = useThemeStore((s) => s.bubbleAiMode)
  // 背景图激活信号（背景图主题/皮肤）：平铺 AI 消息在背景图上必须框起
  // 气泡面（用户裁决：文字不许裸贴背景图；只框气泡区域不糊整块）
  const bgImageActive = useThemeStore((s) => s.bgImageActive)
  const isTool = message.role === 'tool'

  const isSystemMessage = message.role === 'system'

  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const isMessageStreaming = message.status === 'streaming'

  const { data: agents = [] } = useAgentsQuery()
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
      reportError(err instanceof Error ? err.message : String(err), {
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

  /** 工具消息独立渲染：统一走 ActivityCard（与消息流 parts 吸收路径同款满宽卡） */
  if (isTool) {
    const toolName: string = message.toolName || (message.metadata?.name as string | undefined) || '工具'
    const toolStatus: string = message.status || 'completed'
    const toolResult: unknown = message.toolResult || message.metadata?.result || message.metadata?.output
    const toolError: unknown = message.toolError || message.metadata?.error
    const durationMs: unknown = message.durationMs || message.metadata?.duration_ms

    const statusMap: Record<string, MessageToolCall['status']> = {
      completed: 'completed',
      failed: 'failed',
      running: 'running',
      streaming: 'running',
      pending: 'pending',
      cancelled: 'cancelled',
    }

    const activity = toolCallToActivity(
      {
        call_id: message.toolCallId || message.id,
        tool_name: toolName,
        tool_args: (message.metadata?.args as Record<string, unknown> | undefined) ?? {},
        status: statusMap[toolStatus] ?? 'completed',
        result: toolResult,
        resultData: message.toolResultData,
        error: typeof toolError === 'string' ? toolError : undefined,
        duration_ms: typeof durationMs === 'number' ? durationMs : undefined,
        containerTaskId: message.metadata?.containerTaskId as string | undefined,
      },
      {
        onOpenFile: (filePath, recordCtid) =>
          getGlobalOpenFileCallback()(filePath, recordCtid ?? taskId),
      },
    )

    return (
      <div
        className={cn(
          'group hover:bg-muted/30 flex gap-3 px-4 py-2 transition-colors',
          'max-w-[calc(100%-44px)]',
          className,
        )}
        data-testid="message-item"
        data-role="tool"
      >
        <div className="min-w-0 flex-1">
          <ActivityCard activity={activity} />
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
            <User className="h-icon-md w-icon-md" />
          ) : isSystemMessage ? (
            <Bell className="h-icon-md w-icon-md" />
          ) : (
            <Bot className="h-icon-md w-icon-md" />
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
                // 背景图平铺态的气泡面模糊（仅气泡盒内，不糊整块；非激活时
                // 空串 = React 省略该内联属性）
                backdropFilter: '',
              }
              const bubbleCls = cn(
                'overflow-hidden',
                isSystemMessage
                  ? 'w-full border-l-4 border-status-warning/40'
                  : isUser
                    ? 'max-w-full'
                    : 'w-full',
              )
              // AI 平铺模式（DeepSeek/DSH 原生形态）：去气泡盒——透明底、
              // 无圆角/阴影/边框，文字随对话区区域前景（--region-chat-fg）
              if (!isUser && !isSystemMessage && bubbleAiMode === 'flat') {
                if (bgImageActive) {
                  // 背景图上平铺文本不许裸贴：换成
                  // 半透明气泡面——面用皮肤原生气泡令牌（翻译器按皮肤
                  // patches.css 提取 --bubble-ai-bg，跟皮肤颜色一样），
                  // 无皮肤气泡声明的主题回退 --card；局部仅内容区（收拢由
                  // index.css fit-content 规则完成），模糊限定在气泡盒内
                  bubbleStyle.background = 'var(--bubble-ai-bg, color-mix(in srgb, var(--card) 80%, transparent))'
                  bubbleStyle.color = 'var(--bubble-ai-text, var(--foreground))'
                  bubbleStyle.borderRadius = '18px 18px 18px 6px'
                  bubbleStyle.boxShadow = '0 3px 10px rgb(0 0 0 / 0.08)'
                  bubbleStyle.border = '1px solid color-mix(in srgb, var(--border) 50%, transparent)'
                  bubbleStyle.padding = '0.5rem 0.75rem'
                  bubbleStyle.backdropFilter = 'blur(6px)'
                } else {
                  // 空串赋值 = React 省略该内联属性：透明底、无圆角/阴影/边框，
                  // 文字继承对话区区域前景（--region-chat-fg）
                  bubbleStyle.background = 'transparent'
                  bubbleStyle.color = ''
                  bubbleStyle.borderRadius = ''
                  bubbleStyle.boxShadow = ''
                  bubbleStyle.border = ''
                  bubbleStyle.padding = '0.35rem 0.5rem'
                }
              }

              if (isUser) {
                // 插件注入的 Godot 引用消息（<reference source="godot">）：渲染为引用卡片行而非普通气泡
                const refParsed = parseReferenceMessage(renderContext.displayContent || message.content)
                if (refParsed && refParsed.items.length > 0) {
                  return (
                    <div
                      className="flex w-full flex-wrap items-center gap-2 py-0.5"
                      data-role="reference"
                      data-reference-source={refParsed.source}
                    >
                      <span className="text-muted-foreground shrink-0 text-[11px]">
                        Godot 引用{refParsed.scene ? ` · ${refParsed.scene}` : ''}
                      </span>
                      {refParsed.items.map((it) => (
                        <ReferenceChip
                          key={it.path}
                          data={{ kind: 'godot-node', title: it.name, subtitle: `${it.type} @ ${it.path}` }}
                        />
                      ))}
                    </div>
                  )
                }
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
                      // 用户消息统一 markdown 渲染（与 assistant 同款）：
                      // 附件索引以 markdown 引用并入 content（![f](/uploads/x.png)），
                      // 图片/链接由此直接渲染，历史回读天然带引用。
                      <div className="text-sm">
                        <LobeChatMarkdown content={userContent} />
                      </div>
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
                              <Icon className="text-muted-foreground h-icon-md w-icon-md shrink-0" />
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

              // 挂起等待用户交互的 assistant 消息（工具阻塞中、无文本输出）不能整块隐藏：
              // 否则只剩头像/时间戳的空气泡，用户看不到"agent 正在等我审批"。
              const _waitingInteraction = isAssistant && hasPendingInteraction
              // 失败/中断消息不能隐藏：stream_error 标记 error 且无内容时，
              // 隐藏会导致消息凭空消失（错误透传收口）；interrupted（服务端
              // 停止/中断半截消息）同理。
              const _isFailedMessage =
                message.status === 'error' ||
                message.status === 'failed' ||
                message.status === 'interrupted'

              if (
                !isMessageStreaming &&
                renderContext.fragments.length === 0 &&
                !_displayFallback &&
                !_waitingInteraction &&
                !_isFailedMessage
              ) {
                return null
              }

              return (
                <div className={bubbleCls} style={bubbleStyle}>
                  {renderContext.fragments.length === 0 ? (
                    isMessageStreaming || _waitingInteraction ? (
                      <div className="flex items-center gap-2">
                        {hasPendingInteraction ? (
                          <>
                            <MessageSquare className="h-icon-md w-icon-md text-status-info" />
                            <span className="text-sm text-status-info">等待用户响应...</span>
                          </>
                        ) : (
                          <>
                            <Loader2 className="h-icon-md w-icon-md animate-spin" />
                            <span className="text-sm">思考中...</span>
                          </>
                        )}
                      </div>
                    ) : _isFailedMessage ? (
                      <div className="flex items-center gap-2">
                        <AlertCircle className="h-icon-md w-icon-md text-status-error" />
                        <span className="text-sm text-status-error">
                          {message.status === 'error' ? '生成失败，请重试' : '生成已中断'}
                        </span>
                        {message.error && <ErrorSourceBadge source={message.error.source} />}
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
              <Sparkles className="h-icon-xs w-icon-xs" />
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
                onRegenerate={onRegenerate}
                onRollbackTo={onRollbackTo}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  )
})
