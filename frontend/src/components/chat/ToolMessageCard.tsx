/**
 * 工具消息活动卡（role=tool 分支卡组件，自 MessageItem 绞杀者迁移）：
 * toolName/status/result → toolCallToActivity → PresenterScope 内满宽容器
 * + ToolMessageBody（非呈现态直出 ActivityCard；呈现态折叠为静默行动叙事行）。
 * 渲染输出与迁移前逐字节一致（MessageItem.skeleton / MessageItemToolCard 护栏锁定）。
 */
import { memo, useState } from 'react'
import ActivityCard from './ActivityCard'
import { PresenterScope, usePresenter } from './presenterScope'
import { toolCallToActivity } from '@/utils/activityConverter'
import { getGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import { cn } from '@/lib/utils'
import type { ActivityData } from '@/types/activity'
import type { Message } from '@/types/models'
import type { PersonaPossession } from '@/services/schema/modeOptions'

/** tool 消息状态 → ActivityStatus 映射（streaming 与 running 同义） */
const TOOL_STATUS_MAP: Record<string, ActivityData['status']> = {
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
function resolveToolStatus(raw: string): ActivityData['status'] {
  const mapped = TOOL_STATUS_MAP[raw]
  if (mapped) return mapped
  if (!warnedUnknownToolStatuses.has(raw)) {
    warnedUnknownToolStatuses.add(raw)
    console.warn(`[MessageItem] 未知工具消息状态 "${raw}"，按 pending 渲染`)
  }
  return 'pending'
}

/**
 * 呈现态工具消息体：工具卡片沉浸化——默认折叠为一行低调叙事行
 * （`✦ <角色名>的静默行动`；名字缺席回退 `✦ 静默行动`；失败态显示
 * `✦ 行动受挫` 弱警示色），点击行展开原生 ActivityCard 详情（可查证），
 * 再点收回；折叠态为组件内 useState，每消息独立且不持久化。
 * 非呈现态（presenter/附身双未命中）原样直出 ActivityCard，布局与既有零差异。
 */
function ToolMessageBody({ activity, failed }: { activity: ActivityData; failed: boolean }) {
  const presenter = usePresenter()
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

export const ToolMessageCard = memo(function ToolMessageCard({
  message,
  possessActive,
  possessed,
  taskId,
  className = '',
}: {
  message: Message
  possessActive: boolean
  possessed: PersonaPossession | null
  taskId?: string
  className?: string
}) {
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
})
