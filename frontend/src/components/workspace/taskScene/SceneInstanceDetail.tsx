/**
 * 实例运行窗口（面板内视图，实例卡点击 `inline` 目标的落点）
 *
 * 终端实例点开看到的就是「正在跑的命令 + 输出」——直接复用 `buildSceneDetailBlocks`
 * 产出的终端块（`DetailBlock` contentType='terminal'，含命令行、cwd、exit、stdout
 * 与运行中标记），不新写一套输出渲染。桌面/软件/手机实例同构展示最近活动的详情块
 * 与截图缩略图（这些通道没有已声明的运行时页面，面板内视图是它们唯一如实的落点）。
 *
 * 只列该实例最近 `MAX_ROWS` 条调用：运行窗口回答「现在在干什么」，全量复盘在
 * 「活动历史」区（不在这里堆流水）。
 */

import { useState } from 'react'
import { ChevronDown, ChevronRight, X } from '@/assets/icons'
import { DetailBlock } from '@/components/chat/ActivityBlockViews'
import { formatDuration } from '@/types/activity'
import { buildSceneDetailBlocks } from './sceneDetails'
import { formatStartTime, sceneCategoryIcon } from './sceneRowHelpers'
import { SceneThumbnail } from './SceneThumbnail'
import type { SceneActivity } from './sceneActivities'
import type { SceneInstance } from './sceneInstances'
import type { FC } from 'react'

/** 运行窗口里最多铺开的调用条数（其余指向活动历史） */
const MAX_ROWS = 5

export interface SceneInstanceDetailProps {
  instance: SceneInstance
  /** 关闭运行窗口 */
  onClose: () => void
}

/** 单条调用（默认只看最新一条展开，其余按需展开） */
const InstanceActivityRow: FC<{ activity: SceneActivity; defaultExpanded: boolean }> = ({
  activity,
  defaultExpanded,
}) => {
  const [expanded, setExpanded] = useState(defaultExpanded)
  const blocks = buildSceneDetailBlocks(activity)

  return (
    <div className="border-border/60 border-b last:border-b-0" data-testid="instance-activity-row">
      <button
        type="button"
        onClick={() => setExpanded((prev) => !prev)}
        aria-expanded={expanded}
        className="hover:bg-[var(--hover-overlay)] flex w-full items-center gap-1.5 px-2 py-1 text-left transition-colors"
      >
        {expanded ? (
          <ChevronDown className="text-muted-foreground h-icon-sm w-icon-sm flex-shrink-0" />
        ) : (
          <ChevronRight className="text-muted-foreground h-icon-sm w-icon-sm flex-shrink-0" />
        )}
        <span className="text-foreground min-w-0 flex-1 truncate font-mono text-[11px]" title={activity.summary}>
          {activity.summary}
        </span>
        {activity.durationMs !== undefined && (
          <span className="text-muted-foreground/70 flex-shrink-0 text-[10px]">
            {formatDuration(activity.durationMs)}
          </span>
        )}
        <SceneThumbnail activity={activity} containerTaskId={activity.containerTaskId} />
      </button>
      {expanded && (
        <div className="space-y-1.5 px-2 pb-2" data-testid="instance-activity-detail">
          {blocks.length === 0 ? (
            <div className="text-muted-foreground bg-muted/30 rounded px-2 py-1 text-[11px]">
              这次调用没有可展示的详情（结果为空或尚未返回）
            </div>
          ) : (
            blocks.map((block, index) => (
              <DetailBlock key={block.id ?? `${activity.callId}-${index}`} block={block} />
            ))
          )}
        </div>
      )}
    </div>
  )
}

/** 实例运行窗口（实例卡点开的面板内视图） */
export const SceneInstanceDetail: FC<SceneInstanceDetailProps> = ({ instance, onClose }) => {
  const Icon = sceneCategoryIcon(instance.category)
  /** 最近 MAX_ROWS 条（时序倒序展示，最新在上） */
  const rows = [...instance.activities].reverse().slice(0, MAX_ROWS)

  return (
    <section
      className="border-border flex flex-shrink-0 flex-col border-b"
      aria-label="实例运行窗口"
      data-testid="scene-instance-detail"
      data-instance-key={instance.key}
    >
      <div className="flex flex-shrink-0 items-center gap-2 px-3 py-1.5">
        <Icon className="text-muted-foreground h-icon-sm w-icon-sm flex-shrink-0" />
        <span className="text-foreground min-w-0 truncate text-xs font-medium">
          {instance.label}
          {instance.focus && (
            <span className="text-muted-foreground/70 font-normal"> · {instance.focus}</span>
          )}
        </span>
        <span className="text-muted-foreground/60 text-[10px]">
          {instance.activities.length} 次调用
          {instance.inflightCount > 0 ? ` · ${instance.inflightCount} 在途` : ''}
        </span>
        {instance.startedAt && (
          <span className="text-muted-foreground/60 font-mono text-[10px]">
            起 {formatStartTime(instance.startedAt)}
          </span>
        )}
        <button
          type="button"
          onClick={onClose}
          className="text-muted-foreground/60 hover:text-foreground ml-auto flex-shrink-0 rounded p-0.5 transition-colors hover:bg-[var(--hover-overlay)]"
          title="收起运行窗口"
          aria-label="收起运行窗口"
          data-testid="scene-instance-detail-close"
        >
          <X className="h-icon-sm w-icon-sm" />
        </button>
      </div>

      <div className="max-h-[40vh] overflow-auto" data-testid="scene-instance-detail-rows">
        {rows.length === 0 ? (
          <div className="text-muted-foreground px-3 pb-2 text-[11px]">这个实例还没有调用记录</div>
        ) : (
          rows.map((activity, index) => (
            <InstanceActivityRow
              key={activity.callId}
              activity={activity}
              defaultExpanded={index === 0}
            />
          ))
        )}
      </div>

      {instance.activities.length > rows.length && (
        <div className="text-muted-foreground/60 flex-shrink-0 px-3 py-1 text-[10px]">
          只显示最近 {rows.length} 条，更早的调用见下方「活动历史」
        </div>
      )}
    </section>
  )
}
