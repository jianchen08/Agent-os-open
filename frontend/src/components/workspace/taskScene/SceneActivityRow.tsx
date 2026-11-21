/**
 * 现场时间轴单行（类别图标 + 一行文案 + 状态 + 耗时 + 可展开详情）
 *
 * 一行识别 / 可展开 / 有图带图（方案 §3.3.0 统一条目形态）。展开区渲染的是
 * 既有 `DetailBlock`（终端块/差异块/搜索块…），本组件只负责「一行摘要」与
 * 展开骨架，不新写内容渲染。
 *
 * 缩略图（截图类）由 SceneThumbnail 承接（批 3 接入），本组件留 `withThumbnail`
 * 插槽位：有内容就带图，无内容纯动作行。
 */

import { useState } from 'react'
import {
  Ban,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Clock,
  Loader2,
  XCircle,
} from '@/assets/icons'
import { DetailBlock } from '@/components/chat/ActivityBlockViews'
import { formatDuration } from '@/types/activity'
import { buildSceneDetailBlocks, toActivityStatus } from './sceneDetails'
import { formatStartTime, sceneCategoryIcon } from './sceneRowHelpers'
import { SceneThumbnail } from './SceneThumbnail'
import type { SceneActivity } from './sceneActivities'
import type { FC, ReactNode } from 'react'

/** 状态表达：运行中转圈、失败叉、成功勾、取消禁、未知时钟 */
const StatusMark: FC<{ activity: SceneActivity }> = ({ activity }) => {
  const status = toActivityStatus(activity)
  if (status === 'running') {
    return (
      <span className="text-status-running flex items-center gap-1" data-status="running">
        <Loader2 className="h-icon-sm w-icon-sm animate-spin" />
        运行中
      </span>
    )
  }
  if (status === 'failed') {
    return (
      <span className="text-status-error flex items-center gap-1" data-status="failed">
        <XCircle className="h-icon-sm w-icon-sm" />
        失败
      </span>
    )
  }
  if (status === 'cancelled') {
    return (
      <span className="text-muted-foreground flex items-center gap-1" data-status="cancelled">
        <Ban className="h-icon-sm w-icon-sm" />
        已取消
      </span>
    )
  }
  return (
    <span className="text-status-success flex items-center gap-1" data-status="completed">
      <CheckCircle2 className="h-icon-sm w-icon-sm" />
      完成
    </span>
  )
}

/** 手机安全闸拦截标记（status='blocked' 时与失败区分：交还用户 ≠ 出错） */
function isBlockedBySafetyGate(activity: SceneActivity): boolean {
  if (activity.category !== 'phone') return false
  const data = activity.resultData
  if (typeof data === 'object' && data !== null) {
    if ((data as { status?: unknown }).status === 'blocked') return true
  }
  if (typeof activity.result === 'string' && activity.result.includes('"blocked"')) return true
  return false
}

export interface SceneActivityRowProps {
  activity: SceneActivity
  /** 行头右侧附加内容（缺省渲染截图缩略图：有图带图，无图不带） */
  trailing?: ReactNode
  /** 容器任务 ID 兜底（workspace 落盘截图的取流坐标） */
  containerTaskId?: string
  /** 默认是否展开 */
  defaultExpanded?: boolean
}

/** 现场时间轴一行 */
export const SceneActivityRow: FC<SceneActivityRowProps> = ({
  activity,
  trailing,
  containerTaskId,
  defaultExpanded = false,
}) => {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const Icon = sceneCategoryIcon(activity.category)
  const blocked = isBlockedBySafetyGate(activity)

  return (
    <div
      className="border-border/60 border-b last:border-b-0"
      data-testid="scene-row"
      data-scene-kind={activity.category}
      data-scene-call-id={activity.callId}
      data-scene-state={activity.state}
    >
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="hover:bg-[var(--hover-overlay)] flex w-full items-center gap-2 px-2 py-1.5 text-left"
        title={activity.summary}
      >
        <span className="text-muted-foreground flex-shrink-0">
          {expanded ? (
            <ChevronDown className="h-icon-sm w-icon-sm" />
          ) : (
            <ChevronRight className="h-icon-sm w-icon-sm" />
          )}
        </span>
        <Icon className="text-muted-foreground h-icon-sm w-icon-sm flex-shrink-0" />
        <span className="text-foreground min-w-0 flex-1 truncate text-xs">{activity.summary}</span>

        {blocked ? (
          <span
            className="text-status-waiting flex-shrink-0 text-[11px]"
            data-testid="scene-row-blocked"
          >
            已拦截（交还用户）
          </span>
        ) : (
          <span className="flex-shrink-0 text-[11px]">
            <StatusMark activity={activity} />
          </span>
        )}

        <span className="text-muted-foreground/70 flex-shrink-0 font-mono text-[11px]">
          {activity.durationMs !== undefined ? formatDuration(activity.durationMs) : '--'}
        </span>

        {trailing ?? (
          <SceneThumbnail
            activity={activity}
            containerTaskId={containerTaskId ?? activity.containerTaskId}
          />
        )}
      </button>

      {expanded && (
        <div
          className="bg-muted/5 space-y-1.5 px-2 py-1.5"
          data-testid="scene-row-detail"
        >
          <div className="text-muted-foreground flex flex-wrap items-center gap-2 font-mono text-[11px]">
            <span>{activity.toolName}</span>
            {activity.startedAt && (
              <span className="flex items-center gap-1">
                <Clock className="h-icon-xs w-icon-xs" />
                {formatStartTime(activity.startedAt)}
              </span>
            )}
          </div>
          {buildSceneDetailBlocks(activity).map((block, index) => (
            <DetailBlock key={block.id ?? `block-${index}`} block={block} />
          ))}
        </div>
      )}
    </div>
  )
}
