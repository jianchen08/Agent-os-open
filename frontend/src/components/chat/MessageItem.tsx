/** 消息项组件 显示单条消息，支持用户消息和 AI 消息的不同样式 */

import { createContext, memo, useContext, useMemo, useRef, useState } from 'react'
import { toast } from 'sonner'
import {
  AlertCircleIcon as AlertCircle,
  Bell,
  Bot,
  FileCode,
  FileText,
  FileIcon as FileGeneric,
  MessageSquare,
  Sparkles,
  User,
} from '@/assets/icons'
import { CitationFloatBar } from '@/components/chat/CitationFloatBar'
import { useBubbleCitationBar } from '@/components/chat/hooks/useBubbleCitationBar'
import { LobeChatMarkdown } from '@/components/chat/LobeChatMarkdown'
import { ImageGallery } from '@/components/media/ImageGallery'
import { ErrorSourceBadge } from '@/components/shared/ErrorSourceBadge'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { useAgentsQuery } from '@/hooks/queries/useAgentsQuery'
import { useMessageStateCard } from '@/hooks/queries/useMessageStateCard'
import { cn } from '@/lib/utils'
import { usePresenterProfile } from '@/services/api/presenterProfiles'
import { ErrorType, reportError } from '@/services/errorReporting'
import { markdownLinkInterceptor, openFileWithLoader } from '@/services/fileLoaderRegistry'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { buildBubbleCardPayload, matchBubbleCardStyleId } from '@/services/schema/messageCardBody'
import { stripBySpan } from '@/services/schema/messageStateCard'
import { primaryModeTag, type PersonaPossession } from '@/services/schema/modeOptions'
import { loadSessionExecutionOptions } from '@/services/sessionExecutionOptions'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useChatInputStore } from '@/stores/chatInputStore'
import { useInteractionStore } from '@/stores/interactionStore'
import { usePersonaPossessStore } from '@/stores/personaPossessStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { useThemeStore } from '@/stores/themeStore'
import { toolCallToActivity } from '@/utils/activityConverter'
import { formatTimestamp } from '@/utils/format'
import { getGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import { buildTruncatedResultDetailBlock, resolveTruncatedToolResultForDisplay } from '@/utils/toolResultTruncate'
import ActivityCard from './ActivityCard'
import { CompressionOriginalsButton } from './CompressionOriginalsButton'
import useMessageRender from './hooks/useMessageRender'
import { MessageActions } from './MessageActions'
import MessageContentRenderer from './MessageContentRenderer'
import { MessageEditor } from './MessageEditor'
import { MESSAGE_STYLE_METADATA_KEY, PluginMessageCard, resolveMessageStyle } from './PluginMessageCard'
import { parseReferenceMessage, ReferenceChip } from './ReferenceChip'
import { StreamActivityIndicator } from './StreamActivityIndicator'
import { TaskSceneStatusRow } from './TaskSceneStatusRow'
import { ToolTailActivity } from './ToolTailActivity'
import type { MessageItemProps } from './types'
import type { PresenterAvatar, PresenterProfile } from '@/services/api/presenterProfiles'
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

/** 来源注记的角色标签（引用块首行，用户可读口径；自 MessageActions 件二迁入） */
const ROLE_SOURCE_LABELS: Record<MessageItemProps['message']['role'], string> = {
  user: '用户',
  assistant: '助手',
  system: '系统',
  tool: '工具',
}

/** 已警告过的未知状态值（同一未知值只警告一次，长消息流不刷屏） */
const warnedUnknownToolStatuses = new Set<string>()

/** 消息 markdown 附件链接拦截（/uploads 链接改走加载器，防整窗导航死页） */
const attachmentLinkInterceptor = markdownLinkInterceptor()

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
/** 呈现档案上下文（PresenterScope → 头像槽/徽标的单向投递） */
const PresenterContext = createContext<PresenterProfile | null>(null)

/**
 * 附身档 → 呈现档案：附身是临时态（消息本体身份仍是主 agent），仅投递
 * {name, avatar} 供头像槽/徽标消费；origin 标 possess 以区分卡目录解析来源。
 */
function possessionToPresenter(possessed: PersonaPossession): PresenterProfile {
  return { name: possessed.name, avatar: possessed.avatar, origin: 'possess' }
}

/**
 * 呈现档案作用域：assistant 消息带 agentId 或附身激活时挂载（挂载条件由
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
  possessed: PersonaPossession | null
  children: ReactNode
}) {
  const presenter = usePresenterProfile(agentId)
  const profile = presenter ?? (possessed ? possessionToPresenter(possessed) : null)
  return <PresenterContext.Provider value={profile}>{children}</PresenterContext.Provider>
}

/**
 * 呈现头像徽章（assistant 气泡左侧 28px 圆角头像位）：
 * avatar=emoji 串 → emoji 居中 + 浅底；{fg,bg} 色对 → 名字首字 + fg 字色 bg 底
 * （缺色回退主题 secondary 令牌）；avatar=null → 首字 + 默认底。
 * presenter 未命中（非呈现态）时调用方不渲染本组件、走默认头像，布局零变化。
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
        color: emoji ? undefined : (pair?.fg ?? 'var(--secondary-foreground)'),
      }}
    >
      <span className={emoji ? 'text-base leading-none' : 'text-sm font-medium leading-none'}>
        {emoji ?? name.charAt(0)}
      </span>
    </div>
  )
}

/**
 * 消息行首列头像槽：assistant 且呈现档案命中 → 呈现头像徽章；
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
 * assistant 徽标：呈现命中 → 档案名（emoji 形态并入徽标前位，色对/null 形态仅
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
 * 呈现态工具消息体：工具卡片沉浸化——默认折叠为一行低调叙事行
 * （`✦ <角色名>的静默行动`；名字缺席回退 `✦ 静默行动`；失败态显示
 * `✦ 行动受挫` 弱警示色），点击行展开原生 ActivityCard 详情（可查证），
 * 再点收回；折叠态为组件内 useState，每消息独立且不持久化。
 * 非呈现态（presenter/附身双未命中）原样直出 ActivityCard，布局与既有零差异。
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
        data-testid="presenter-tool-narrative"
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

/**
 * 本体卡体（ADR 2026-10-14-message-card-body-form）：头像载荷在卡组件内解析
 * （呈现档案源既有——agentId → data_cards；附身激活时附身档覆盖，与
 * PresenterScope 同优先序）。仅本体形态行挂载本组件——非 RP 会话/非命中行
 * 零挂载零请求（兼容态数据面零变化），故 presenter hook 不上提至 MessageItem
 * 顶层（无条件 useQuery 会牵连全部无 Provider 的既有渲染测试）。
 */
function BubbleCardBody({
  message,
  styleId,
  content,
  isUser,
  possessActive,
  possessed,
}: {
  message: MessageItemProps['message']
  styleId: string
  content: string
  isUser: boolean
  possessActive: boolean
  possessed: PersonaPossession | null
}) {
  const presenterProfile = usePresenterProfile(isUser ? undefined : (message.agentId ?? undefined))
  const presenter =
    presenterProfile ?? (possessActive && possessed ? possessionToPresenter(possessed) : null)
  return (
    <PluginMessageCard
      instanceKey={message.id}
      styleId={styleId}
      fit="auto"
      message={{
        content,
        metadata: {
          card: buildBubbleCardPayload(
            isUser ? 'user' : 'assistant',
            message.status,
            message.agentId ?? undefined,
            presenter,
          ),
        },
      }}
    />
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
  readOnly = false,
  streamAnchorMs,
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
  // 消息气泡毛玻璃开关（主题级 effects.messageGlass）：皮肤激活期皮肤即
  // "主题"（皮肤清单未声明该字段 → 缺省开，玻璃面是皮肤 flat 气泡的既有
  // 观感基线，base 预设的声明不牵连皮肤）；内置主题按预设声明，缺省开
  const messageGlassOn = useThemeStore((s) =>
    s.activePluginTheme ? true : (s.themeConfig?.effects?.messageGlass ?? true),
  )
  const isTool = message.role === 'tool'

  const isSystemMessage = message.role === 'system'

  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const isMessageStreaming = message.status === 'streaming'

  // ────────────────────────────────────────────
  // 选中文字引用/评审浮层（R4 件③，用户裁定：消息流入口=工作区制品同形态）
  // 气泡内选中文字 → 选区上方弹 CitationFloatBar（fixed 视口坐标 portal）；
  // R5 落点卡片化：动作产 CitationItem 入列（chatInputStore.citationsByDraftKey，
  // draftKey 与 ChatContainer 接线同构 = activeTabId || activeSessionId），
  // markdown 块引用合成移到发送装配期（composeSendParams），发送产物不变。
  // ────────────────────────────────────────────
  const bubbleRef = useRef<HTMLDivElement | null>(null)
  const citationBarEnabled = !readOnly && !!activeSessionId
  const { bar: citationBar, closeBar: closeCitationBar, handleBubbleMouseUp } =
    useBubbleCitationBar(bubbleRef, citationBarEnabled)
  const addCitation = useChatInputStore((s) => s.addCitation)
  const activeTabId = useAgentTabStore((s) => s.activeTabId)
  const citationDraftKey = activeTabId || activeSessionId || ''

  /** 引用：选中文字定格快照入列（kind=message，title=角色标签；发送装配期
   *  以 createdAt 还原来源时间注记） */
  const handleCitationQuote = () => {
    if (!citationBar) return
    addCitation(citationDraftKey, {
      kind: 'message',
      title: ROLE_SOURCE_LABELS[message.role] ?? message.role,
      excerpt: citationBar.selectedText,
    })
    closeCitationBar()
    window.getSelection()?.removeAllRanges()
  }

  /** 评审：同引用入列 + 评语标记（意见为空时 store 侧不落 comment 字段） */
  const handleCitationReview = (comment: string) => {
    if (!citationBar) return
    addCitation(citationDraftKey, {
      kind: 'message',
      title: ROLE_SOURCE_LABELS[message.role] ?? message.role,
      excerpt: citationBar.selectedText,
      comment,
    })
    closeCitationBar()
    window.getSelection()?.removeAllRanges()
  }
  // 「压缩中」：上下文压缩是分钟级同步调用，期间无流式产出——占位气泡须显示
  // 「压缩中」而非通用「思考中」，用户才能区分在等模型还是在等压缩
  // （用户裁定 2026-10-28）。按活跃管道判定（压缩发生在当前对话管道内）。
  const isCompressing = usePipelineMessageStore((s) => {
    const pipelineId = s.activePipelineId
    return pipelineId ? !!s.compressingByPipeline[pipelineId] : false
  })

  const { data: agents = [] } = useAgentsQuery()
  const agent = message.agentId ? agents.find((a) => a.id === message.agentId) : null

  // 附身态订阅：选择器直返 possessed，null 恒稳定引用，未附身零额外重渲染
  const possessed = usePersonaPossessStore((s) => s.possessed)
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

  // 状态标记切除（2026-09-28 设计 §5b，渲染形态 2026-10-17 退役 webview 卡）：
  // 三道门（最新 assistant 非流式/解析步端点载荷在场/插件 message_cards 声明
  // 命中——宿主零样式 id 硬编码）。正文按 span 精确切除标记段（标记可能在中间，
  // 后半正文不可丢）；状态呈现归工作区玩法页（状态快照/状态条），消息流不再
  // 尾随状态卡。无载荷/无声明 = null，渲染输出与既有逐字节一致。
  const stateCard = useMessageStateCard(isLast && isAssistant && !isMessageStreaming)
  const stateCardMessage = stateCard
    ? { ...message, content: stripBySpan(message.content, stateCard.span) }
    : message

  const renderContext = useMessageRender({
    message: stateCardMessage,
    isLast,
    isGenerating,
    versionContent,
    taskId,
  })

  // 本体形态分发门（ADR 2026-10-14-message-card-body-form）：会话 mode 绑定 →
  // message_cards bubble 声明命中 → 该会话全部已定稿 user/assistant 消息行本体
  // （头像+气泡+正文）交插件卡渲染；流式期间不接（宿主原生渲染零改动，刀3 降格
  // 裁定）。显式 metadata.message_style 卡（上方分支）优先于本体形态。降级态/
  // 等待占位/附件/引用片/非纯文本片段行保持宿主原生（卡不吞既有功能面）。
  const sessionMode = useMemo(() => {
    const sid = message.sessionId || activeSessionId
    return sid ? primaryModeTag(loadSessionExecutionOptions(sid)?.modeBinding?.mode) || null : null
  }, [message.sessionId, activeSessionId])
  const bubbleStyleId = matchBubbleCardStyleId(
    contributionRegistry.getAllMessageCards(),
    sessionMode,
  )
  const bubbleDegraded =
    message.status === 'error' || message.status === 'failed' || message.status === 'interrupted'
  const bubbleWaitingEmpty = isAssistant && hasPendingInteraction && !message.content.trim()
  const bubbleRefParsed =
    isUser ? parseReferenceMessage(renderContext.displayContent || message.content) : null
  const bubbleHasExtras =
    (isUser && (message.attachments?.length ?? 0) > 0) ||
    (bubbleRefParsed !== null && bubbleRefParsed.items.length > 0) ||
    renderContext.fragments.some((f) => f.type !== 'text')
  const bubbleContent = renderContext.displayContent || stateCardMessage.content
  const bubbleRowActive =
    bubbleStyleId !== null &&
    resolveMessageStyle(bubbleStyleId) !== null &&
    (isUser || isAssistant) &&
    !isMessageStreaming &&
    !isEditing &&
    !bubbleDegraded &&
    !bubbleWaitingEmpty &&
    !bubbleHasExtras &&
    bubbleContent.trim() !== ''

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
    // 截断标记解析（数据面瘦身批次 1.2）：结果体超阈值时是「预览+标记」，
    // 标记对象不进渲染分支；展开只显示与 LLM 所见一致的预览（2026-11-18 裁定），
    // 取回的全量消息按 id 替换桶内截断版，本卡随 store 更新显示全量。
    const rawToolResult: unknown = message.toolResult || message.metadata?.result || message.metadata?.output
    const resolvedTool = resolveTruncatedToolResultForDisplay(rawToolResult, message.toolResultData)
    const toolError: unknown = message.toolError || message.metadata?.error
    const durationMs: unknown = message.durationMs || message.metadata?.duration_ms

    let activity = toolCallToActivity(
      {
        call_id: message.toolCallId || message.id,
        tool_name: toolName,
        tool_args: (message.metadata?.args as Record<string, unknown> | undefined) ?? {},
        status: resolvedStatus,
        result: resolvedTool.result,
        resultData: resolvedTool.resultData,
        error: typeof toolError === 'string' ? toolError : undefined,
        duration_ms: typeof durationMs === 'number' ? durationMs : undefined,
        containerTaskId: message.metadata?.containerTaskId as string | undefined,
      },
      {
        onOpenFile: (filePath, recordCtid) =>
          getGlobalOpenFileCallback()(filePath, recordCtid ?? taskId),
      },
    )
    if (resolvedTool.mark) {
      const mark = resolvedTool.mark
      activity = {
        ...activity,
        details: [
          ...(activity.details ?? []),
          // 2026-11-18 用户裁定：展开只显示与 LLM 所见一致的预览（不回取全文）
          buildTruncatedResultDetailBlock(mark),
        ],
      }
    }

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
            'group hover:bg-muted/30 flex flex-col items-start gap-1 px-4 py-1.5 transition-colors',
            'w-full',
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
          <div className="min-w-0 w-full">
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

  // 元信息行（呈现徽标/时间戳/操作钮）：本体形态行与宿主原生行同源共用
  // （观感衔接锚——流式原生行与定稿卡行并存时元信息行逐像素一致）
  const metaRow = (
    <div
      className={cn(
        'text-muted-foreground mt-1.5 flex items-center gap-3 text-xs',
        isUser ? 'flex-row-reverse' : '',
      )}
    >
      {isAssistant && <PresenterAwareBadge agent={agent ?? null} />}

      <span className="text-muted-foreground/70" data-testid="message-time">{formatTimestamp(message.timestamp)}</span>

      {!readOnly && activeSessionId && (
        <div className="flex items-center gap-2">
          {/* 操作组（复制/编辑/回退/重新生成）hover 才显形；‹i/n› 分支切换器
              常驻可见（它是到达历史代版本的唯一入口，藏进 hover 组内等于
              「没有入口」——用户实证：重试多次仍找不到左右切换按钮） */}
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
  )

  const row = (
    <div
      className={cn(
        'group flex flex-col gap-1 px-4 py-1.5 transition-colors',
        isUser ? 'items-end' : 'items-start',
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
          isUser ? 'max-w-[80%] sm:max-w-[75%]' : 'w-full',
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
                ? 'var(--bubble-user-shadow)'
                : 'var(--bubble-ai-shadow)',
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
                  ? 'var(--bubble-ai-shadow)'
                  : isUser
                    ? 'var(--bubble-user-shadow)'
                    : 'var(--bubble-ai-shadow)',
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
                  // 背景图上平铺文本不许裸贴：恒有气泡面（用户裁决）——面用
                  // 皮肤原生气泡令牌（翻译器按皮肤 patches.css 提取
                  // --bubble-ai-bg，跟皮肤颜色一样），无令牌回退 --card；
                  // 局部仅内容区（收拢由 index.css fit-content 规则完成）。
                  // 面是否带毛玻璃（blur+玻璃描边）由主题 effects.messageGlass
                  // 决定；关=去 blur/描边留面（不裸贴仍在）。
                  // 回退面/描边一律 hsl(var(--x)) 原样消费、禁追加 alpha：
                  // backdropContrast 管线在背景图上下文会把 --card/--border
                  // 改写为自带 slash alpha 的形态（面/线 alpha 按真实背景
                  // 包络计算保 AA），再拼 `/ 0.8` 会双斜杠非法（color-mix 基
                  // 三元组同理非法=R22 死声明根因）；无管线时值即纯三元组，
                  // 两种形态 hsl() 皆合法。
                  bubbleStyle.background = 'var(--bubble-ai-bg, hsl(var(--card)))'
                  bubbleStyle.color = 'var(--bubble-ai-text, var(--foreground))'
                  bubbleStyle.borderRadius = '18px 18px 18px 6px'
                  bubbleStyle.boxShadow = 'var(--shadow-sm)'
                  bubbleStyle.border = messageGlassOn ? '1px solid hsl(var(--border))' : ''
                  bubbleStyle.padding = '0.5rem 0.75rem'
                  bubbleStyle.backdropFilter = messageGlassOn ? 'blur(6px)' : ''
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
                  <div
                    ref={bubbleRef}
                    onMouseUp={handleBubbleMouseUp}
                    className={bubbleCls}
                    style={bubbleStyle}
                    data-bubble={isUser ? 'user' : 'ai'}
                  >
                    {userContent && (
                      // 用户消息统一 markdown 渲染（与 assistant 同款）：
                      // 附件索引以 markdown 引用并入 content（![f](/uploads/x.png)），
                      // 图片/链接由此直接渲染，历史回读天然带引用。
                      <div className="text-sm">
                        <LobeChatMarkdown content={userContent} onLinkClick={attachmentLinkInterceptor} />
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
                                  void openFileWithLoader({
                                    id: att.id,
                                    name: att.name || '文件',
                                    url: att.url,
                                    mime: mime || undefined,
                                  })
                                }
                              }}
                              className="bg-background/60 hover:bg-background text-foreground flex w-full items-center gap-2 rounded-lg border border-border/30 px-2 py-1.5 text-left text-sm transition-colors"
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
              const _rawFallback = renderContext.displayContent || stateCardMessage.content
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
                <div
                  ref={bubbleRef}
                  onMouseUp={handleBubbleMouseUp}
                  className={bubbleCls}
                  style={bubbleStyle}
                  data-bubble={isUser ? 'user' : 'ai'}
                >
                  {renderContext.fragments.length === 0 ? (
                    isMessageStreaming || _waitingInteraction ? (
                      <div className="flex items-center gap-2">
                        {hasPendingInteraction ? (
                          <>
                            <MessageSquare className="h-icon-md w-icon-md text-status-info" />
                            <span className="text-sm text-status-info">等待用户响应...</span>
                          </>
                        ) : (
                          <StreamActivityIndicator
                            label={isCompressing ? '压缩中' : '执行中'}
                            anchorMs={streamAnchorMs}
                          />
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
                      streamAnchorMs={streamAnchorMs}
                    />
                  )}
                  {/* 空内容占位已承载「执行中」；仅在有可见片段时补底部活动行，
                      避免同一消息出现两条执行中指示（规格三） */}
                  {renderContext.fragments.length > 0 && (
                    (isLast && isAssistant ? (
                        // 最新 AI 气泡：状态行兼作任务现场主入口 + 一条现场预览
                        // （用户裁定 2026-11-20：消息流为任务现场主入口）
                        <TaskSceneStatusRow
                          taskId={taskId}
                          streaming={renderContext.isStreaming}
                          pendingInteraction={hasPendingInteraction}
                          compressing={isCompressing}
                          streamAnchorMs={streamAnchorMs}
                        />
                      ) : (
                        <ToolTailActivity streaming={renderContext.isStreaming} parts={message.parts} pendingInteraction={hasPendingInteraction} compressing={isCompressing} streamAnchorMs={streamAnchorMs} />
                      ))
                  )}
                </div>
              )
            })()}
          </>
        )}

        {metaRow}
        {/* 选中文字引用/评审浮层（fixed portal，视口坐标）：气泡选区快照见上 */}
        {citationBarEnabled && citationBar && (
          <CitationFloatBar
            positioning="fixed"
            position={{ x: citationBar.x, y: citationBar.y }}
            onQuote={handleCitationQuote}
            onReview={handleCitationReview}
            onClose={closeCitationBar}
          />
        )}
      </div>
    </div>
  )

  // 本体形态行（ADR 2026-10-14-message-card-body-form）：头像槽/气泡盒/正文整体
  // 交插件卡（fit=auto 自适应高，载荷信封 metadata.card）；元信息行与宿主同源。
  const bodyRow = bubbleRowActive ? (
    <div
      className={cn(
        'group flex flex-col gap-1 px-4 py-1.5 transition-colors',
        isUser ? 'items-end' : 'items-start',
        'hover:bg-muted/30',
        className,
      )}
      data-testid="message-item"
      data-role={message.role}
      data-message-card-body={bubbleStyleId ?? undefined}
    >
      <BubbleCardBody
        message={message}
        styleId={bubbleStyleId ?? ''}
        content={bubbleContent}
        isUser={isUser}
        possessActive={possessActive}
        possessed={possessActive ? possessed : null}
      />
      {metaRow}
    </div>
  ) : (
    row
  )

  // 状态标记已切除（webview 状态卡退役，2026-10-17）：stateCard 只承载标记段
  // span 切除（stateCardMessage），消息流不再尾随任何状态卡件——状态呈现归
  // 工作区玩法页（状态快照/状态条）。

  // 扮演呈现作用域：assistant+agentId（卡身份解析）或附身激活（限活跃管道
  // 会话）挂载，其余消息行不引入作用域，渲染输出与既有完全一致；
  // possessed 按作用域门控传入，作用域外不泄漏进 agentId 挂载的档案解析
  if (isAssistant && (message.agentId || possessActive)) {
    return (
      <PresenterScope
        agentId={message.agentId ?? undefined}
        possessed={possessActive ? possessed : null}
      >
        {bodyRow}
      </PresenterScope>
    )
  }
  return bodyRow
})
