/** 消息项组件 显示单条消息，支持用户消息和 AI 消息的不同样式 */

import { createContext, memo, useContext, useEffect, useRef, useState } from 'react'
import { toast } from 'sonner'
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
import { LobeChatMarkdown } from '@/components/chat/LobeChatMarkdown'
import { ImageGallery } from '@/components/media/ImageGallery'
import { ErrorSourceBadge } from '@/components/shared/ErrorSourceBadge'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { useAgentsQuery } from '@/hooks/queries/useAgentsQuery'
import { cn } from '@/lib/utils'
import { usePresenterProfile } from '@/services/api/presenterProfiles'
import { openAttachment } from '@/services/attachmentOpener'
import { ErrorType, reportError } from '@/services/errorReporting'
import { useInteractionStore } from '@/stores/interactionStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useRoleplayPossessStore } from '@/stores/roleplayPossessStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useThemeStore } from '@/stores/themeStore'
import { toolCallToActivity } from '@/utils/activityConverter'
import { formatTimestamp } from '@/utils/format'
import { getGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import ActivityCard from './ActivityCard'
import { CompressionOriginalsButton } from './CompressionOriginalsButton'
import useMessageRender from './hooks/useMessageRender'
import { MessageActions } from './MessageActions'
import MessageContentRenderer from './MessageContentRenderer'
import { MESSAGE_STYLE_METADATA_KEY, PluginMessageCard, resolveMessageStyle } from './PluginMessageCard'
import { parseReferenceMessage, ReferenceChip } from './ReferenceChip'
import type { MessageItemProps } from './types'
import type { PresenterAvatar, PresenterProfile } from '@/services/api/presenterProfiles'
import type { RoleplayPossession } from '@/services/schema/modeOptions'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'
import type { ReactNode } from 'react'

/** tool 消息状态 → ActivityStatus 映射（streaming 与 running 同义） */
const TOOL_STATUS_MAP: Record<string, MessageToolCall['status']> = {
  completed: 'completed',
  failed: 'failed',
  running: 'running',
  streaming: 'running',
  pending: 'pending',
  cancelled: 'cancelled',
}

/** 已警告过的未知状态值（同一未知值只警告一次，长消息流不刷屏） */
const warnedUnknownToolStatuses = new Set<string>()

/**
 * 解析 tool 消息状态：未知值不猜 completed（未知 ≠ 成功），
 * 归入 pending 并 console.warn 提示词表外的新状态。
 */
function resolveToolStatus(raw: string): MessageToolCall['status'] {
  const mapped = TOOL_STATUS_MAP[raw]
  if (mapped) return mapped
  if (!warnedUnknownToolStatuses.has(raw)) {
    warnedUnknownToolStatuses.add(raw)
    console.warn(`[MessageItem] 未知工具消息状态 "${raw}"，按 pending 渲染`)
  }
  return 'pending'
}

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

/** 扮演呈现档案上下文（PresenterScope → 头像槽/徽标的单向投递） */
const PresenterContext = createContext<PresenterProfile | null>(null)

/**
 * 附身档 → 呈现档案：附身是临时态（消息本体身份仍是主 agent），仅投递
 * {name, avatar} 供头像槽/徽标消费；origin 标 possess 以区分卡目录解析来源。
 */
function possessionToPresenter(possessed: RoleplayPossession): PresenterProfile {
  return { name: possessed.name, avatar: possessed.avatar, origin: 'possess' }
}

/**
 * 扮演呈现档案作用域：assistant 消息带 agentId 或附身激活时挂载（挂载条件由
 * 调用方裁定）。呈现优先级：消息自带模式卡键 → presenter 解析优先（卡身份是
 * 消息的永久属性）；presenter 未命中且附身激活 → 附身档覆盖（解除即回退）。
 * agentId 缺席时 usePresenterProfile 恒 null（零请求）。
 */
function PresenterScope({
  agentId,
  possessed,
  children,
}: {
  agentId: string | undefined
  possessed: RoleplayPossession | null
  children: ReactNode
}) {
  const presenter = usePresenterProfile(agentId)
  const profile = presenter ?? (possessed ? possessionToPresenter(possessed) : null)
  return <PresenterContext.Provider value={profile}>{children}</PresenterContext.Provider>
}

/**
 * 扮演头像徽章（assistant 气泡左侧 28px 圆角头像位）：
 * avatar=emoji 串 → emoji 居中 + 浅底；{fg,bg} 色对 → 名字首字 + fg 字色 bg 底
 * （缺色回退主题 secondary 令牌）；avatar=null → 首字 + 默认底。
 * presenter 未命中（非扮演）时调用方不渲染本组件、走默认头像，布局零变化。
 */
function PresenterAvatarBadge({ name, avatar }: { name: string; avatar: PresenterAvatar }) {
  const emoji = typeof avatar === 'string' && avatar.trim() !== '' ? avatar : null
  const pair = avatar && typeof avatar !== 'string' ? avatar : null
  return (
    <div
      className="flex h-7 w-7 flex-shrink-0 items-center justify-center rounded-lg shadow-sm"
      data-testid="presenter-avatar"
      style={{
        background: emoji ? 'var(--secondary)' : (pair?.bg ?? 'var(--secondary)'),
        color: emoji ? undefined : pair?.fg,
      }}
    >
      <span className={emoji ? 'text-base leading-none' : 'text-sm font-medium leading-none'}>
        {emoji ?? name.charAt(0)}
      </span>
    </div>
  )
}

/**
 * 消息行首列头像槽：assistant 且扮演档案命中 → 扮演头像徽章；
 * 其余（user/system/未命中）→ 默认头像（形态与既有渲染一致）。
 */
function AvatarSlot({ isUser, isSystemMessage }: { isUser: boolean; isSystemMessage: boolean }) {
  const presenter = useContext(PresenterContext)
  if (!isUser && !isSystemMessage && presenter) {
    return <PresenterAvatarBadge name={presenter.name} avatar={presenter.avatar} />
  }
  return (
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
  )
}

/**
 * assistant 徽标：扮演命中 → 卡名（emoji 形态并入徽标前位，色对/null 形态仅
 * 卡名，不动徽标底色）；未命中 → 注册表徽标（Sparkles+agent 名，原样）。
 * presenter 与 agent 双未命中不渲染。
 */
function PresenterAwareBadge({ agent }: { agent: { name: string } | null }) {
  const presenter = useContext(PresenterContext)
  if (!presenter && !agent) return null
  return (
    <span className="inline-flex items-center gap-1 rounded-lg bg-[var(--badge-info-bg)] px-2 py-0.5 text-xs text-[var(--badge-info-text)]">
      {presenter ? (
        <>
          {typeof presenter.avatar === 'string' && presenter.avatar && (
            <span aria-hidden="true">{presenter.avatar}</span>
          )}
          <span className="font-medium">{presenter.name}</span>
        </>
      ) : (
        <>
          <Sparkles className="h-icon-xs w-icon-xs" />
          <span className="font-medium">{agent?.name}</span>
        </>
      )}
    </span>
  )
}

/**
 * 扮演呈现态工具消息体：工具卡片沉浸化——默认折叠为一行低调叙事行
 * （`✦ <角色名>的静默行动`；名字缺席回退 `✦ 静默行动`；失败态显示
 * `✦ 行动受挫` 弱警示色），点击行展开原生 ActivityCard 详情（可查证），
 * 再点收回；折叠态为组件内 useState，每消息独立且不持久化。
 * 非扮演态（presenter/附身双未命中）原样直出 ActivityCard，布局与既有零差异。
 */
function ToolMessageBody({ activity, failed }: { activity: ActivityData; failed: boolean }) {
  const presenter = useContext(PresenterContext)
  const [expanded, setExpanded] = useState(false)
  if (!presenter) {
    return <ActivityCard activity={activity} />
  }
  return (
    <>
      <button
        type="button"
        data-testid="roleplay-tool-narrative"
        onClick={() => setExpanded((v) => !v)}
        className={cn(
          'cursor-pointer rounded px-0.5 text-left text-xs transition-colors',
          failed ? 'text-status-warning/70' : 'text-muted-foreground',
        )}
      >
        {failed ? '✦ 行动受挫' : presenter.name ? `✦ ${presenter.name}的静默行动` : '✦ 静默行动'}
      </button>
      {expanded && <ActivityCard activity={activity} />}
    </>
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
  segmentSwitcherBaseSeq,
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

  // 附身态订阅：选择器直返 possessed，null 恒稳定引用，未附身零额外重渲染
  const possessed = useRoleplayPossessStore((s) => s.possessed)
  // 附身作用域：消息对象无管道归属字段，以「消息 session === 活跃管道所属
  // session」判定（主聊天视图消息恒取自活跃管道）；历史会话/其他会话的气泡
  // 不随附身切换。布尔选择器，false 稳定不触发重渲染。
  const possessInScope = usePipelineMessageStore((s) => {
    const activePipelineSession = s.activePipelineId
      ? s.pipelineSessionMap[s.activePipelineId]
      : undefined
    return activePipelineSession === message.sessionId
  })
  /** 附身覆盖生效 = 附身激活且消息属活跃管道会话 */
  const possessActive = possessed !== null && possessInScope

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
    // AI 消息编辑一期：内核无 assistant 消息内容更新通道（regenerate 的
    // new_content 仅收 user 消息、segment_activate 只激活既有段），诚实提示
    // 不落库，且绝不走 user 编辑重发链路（其目标是 user 消息截断重跑）
    if (isAssistant) {
      setIsEditing(false)
      toast.error('消息编辑通道未接线')
      return
    }
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

  /**
   * 工具消息独立渲染：非扮演态统一走 ActivityCard 满宽卡（与消息流 parts 吸收
   * 路径同款）；扮演呈现态（presenter 命中或附身生效）由 ToolMessageBody 特化
   * 为折叠叙事行，工具卡片沉浸化。作用域按消息 agentId（卡键）与附身态挂载，
   * presenter/附身双未命中时其内直出原生卡，渲染输出与既有逐字节一致。
   */
  if (isTool) {
    const toolName: string = message.toolName || (message.metadata?.name as string | undefined) || '工具'
    const toolStatus: string = message.status || 'completed'
    const resolvedStatus = resolveToolStatus(toolStatus)
    const toolResult: unknown = message.toolResult || message.metadata?.result || message.metadata?.output
    const toolError: unknown = message.toolError || message.metadata?.error
    const durationMs: unknown = message.durationMs || message.metadata?.duration_ms

    const activity = toolCallToActivity(
      {
        call_id: message.toolCallId || message.id,
        tool_name: toolName,
        tool_args: (message.metadata?.args as Record<string, unknown> | undefined) ?? {},
        status: resolvedStatus,
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
      <PresenterScope
        agentId={message.agentId ?? undefined}
        possessed={possessActive ? possessed : null}
      >
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
            <ToolMessageBody activity={activity} failed={resolvedStatus === 'failed'} />
          </div>
        </div>
      </PresenterScope>
    )
  }

  // 消息卡路由（模式体系 §5.0 通用能力）：非流式 assistant / system 消息携带
  // metadata.message_style 且 registry 有声明 → 通用 webview 消息卡容器；
  // 无声明（禁用/未声明）同源消失，回退下方默认渲染。流式期间不路由
  // （正文仍在到达，卡片属终态渲染，完成后接管）。system 覆盖压缩块消息
  // （消息段模型 §5.1：role=system 块 + metadata.message_style=compression_card，
  // 卡渲染 + 宿主桥数据注入；带段引用的块消息另有宿主侧「查看原始」入口）。
  if ((isAssistant || isSystemMessage) && !isMessageStreaming) {
    const styleId = message.metadata?.[MESSAGE_STYLE_METADATA_KEY]
    if (typeof styleId === 'string' && resolveMessageStyle(styleId)) {
      return (
        <div
          className={cn(
            'group hover:bg-muted/30 flex gap-3 px-4 py-2 transition-colors',
            'max-w-[calc(100%-44px)]',
            className,
          )}
          data-testid="message-item"
          data-role={message.role}
          data-message-style={styleId}
        >
          <Avatar
            className={cn(
              'h-8 w-8 flex-shrink-0 rounded-xl shadow-sm',
              isSystemMessage
                ? 'bg-status-warning/15 text-status-warning'
                : 'bg-secondary text-secondary-foreground',
            )}
          >
            <AvatarFallback className="rounded-xl text-sm font-medium">
              {isSystemMessage ? (
                <Bell className="h-icon-md w-icon-md" />
              ) : (
                <Bot className="h-icon-md w-icon-md" />
              )}
            </AvatarFallback>
          </Avatar>
          <div className="min-w-0 flex-1">
            <PluginMessageCard
              instanceKey={message.id}
              styleId={styleId}
              message={{ content: message.content, metadata: message.metadata }}
            />
            {/* 宿主侧「查看原始 N 条」：卡上行桥未放行内核段端点（web/cards/
                compression.html 取数缺口），段取数由宿主承担（只读，不激活） */}
            {isSystemMessage && <CompressionOriginalsButton message={message} />}
          </div>
        </div>
      )
    }
  }

  const row = (
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
      <AvatarSlot isUser={isUser} isSystemMessage={isSystemMessage} />

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
                // 插件注入的引用消息（<reference source="...">）：渲染为引用卡片行而非普通气泡。
                // 协议源无关（ADR 2026-09-10）：source 从内容解析，任何插件域的引用都可渲染
                const refParsed = parseReferenceMessage(renderContext.displayContent || message.content)
                if (refParsed && refParsed.items.length > 0) {
                  return (
                    <div
                      className="flex w-full flex-wrap items-center gap-2 py-0.5"
                      data-role="reference"
                      data-reference-source={refParsed.source}
                    >
                      <span className="text-muted-foreground shrink-0 text-[11px]">
                        {refParsed.source} 引用{refParsed.scene ? ` · ${refParsed.scene}` : ''}
                      </span>
                      {refParsed.items.map((it) => (
                        <ReferenceChip
                          key={it.path}
                          data={{ kind: `${refParsed.source}-node`, title: it.name, subtitle: `${it.type} @ ${it.path}` }}
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
          {isAssistant && <PresenterAwareBadge agent={agent ?? null} />}

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
                segmentSwitcherBaseSeq={segmentSwitcherBaseSeq}
              />
            </div>
          )}
        </div>
      </div>
    </div>
  )

  // 扮演呈现作用域：assistant+agentId（卡身份解析）或附身激活（限活跃管道
  // 会话）挂载，其余消息行不引入作用域，渲染输出与既有完全一致；
  // possessed 按作用域门控传入，作用域外不泄漏进 agentId 挂载的档案解析
  if (isAssistant && (message.agentId || possessActive)) {
    return (
      <PresenterScope
        agentId={message.agentId ?? undefined}
        possessed={possessActive ? possessed : null}
      >
        {row}
      </PresenterScope>
    )
  }
  return row
})
