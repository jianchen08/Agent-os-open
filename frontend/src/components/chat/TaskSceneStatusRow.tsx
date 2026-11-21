/**
 * 消息气泡状态行（最新 AI 气泡下方：执行中 · N.Ns + 任务现场入口 + 现场预览）
 *
 * 用户裁定 2026-11-20：「把任务现场的按钮放到气泡下面的状态里面，和其他很多 Agent
 * 是一样的，就是放到最新气泡的状态按钮那里。然后还可以展示一个条目，用来显示它的
 * 预览。」——消息流成为任务现场的主入口，任务管理列表/右上角浮动入口不再重复承担。
 *
 * 与既有「单一状态指示点」契约（2026-11-19）的关系：
 * - 「执行中 · N.Ns」仍由 `StreamActivityIndicator` 唯一承担，文案不变（压缩中例外）；
 * - 本组件只在其右侧追加两个元素，不新增第二处状态文字；
 * - 三者在同一行内按容器宽度 `flex-wrap` 换行，预览条目 `min-w-0 truncate`，
 *   窄视口不挤成一团（移动端入口图标化、预览整条退场）。
 *
 * 预览数据面与现场面板同源（`pipelineMessageStore` → `extractSceneActivities`），
 * 复用 `buildSceneInstances` / `buildFileChanges` 纯函数——不在消息层另造一套统计。
 *
 * 参照形态：DSH 在每个 turn 尾部给「产物 chips」+ 任务会话入口（
 * `ui-deliverables/src/client/ProducedFiles.tsx`、`ui-jobs/src/client/JobListAction.tsx`）；
 * ZCode 在状态面板折叠态只报一条最高优先级摘要（`runningBackgroundTasksMiniValue`）。
 * 本行 = 「状态点 + 入口 + 一条摘要」，与该两家的 turn 尾形态同构。
 */

import { useMemo } from 'react'
import { Zap } from '@/assets/icons'
import { openTaskScene } from '@/components/chat/TaskSceneEntryButton'
import { extractSceneActivities } from '@/components/workspace/taskScene/sceneActivities'
import { buildFileChanges } from '@/components/workspace/taskScene/sceneDiff'
import { isScreenshotActivity } from '@/components/workspace/taskScene/sceneImageSource'
import { buildSceneInstances, liveInstances } from '@/components/workspace/taskScene/sceneInstances'
import { useIsMobileViewport } from '@/hooks/useIsMobileViewport'
import { cn } from '@/lib/utils'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { buildScenePreviewText } from './sceneStatusPreview'
import { StreamActivityIndicator } from './StreamActivityIndicator'
import type { Message } from '@/types/models'
import type { FC } from 'react'

/** 空数组常量（zustand 选择器稳定引用） */
const EMPTY_MESSAGES: readonly Message[] = []

export interface TaskSceneStatusRowProps {
  /** 任务坐标（任务 = 管道，同源同值；空 = 不提供入口） */
  taskId?: string
  /** 任务名（现场页签标题同源） */
  title?: string
  /** 归属会话 */
  sessionId?: string
  /** 是否流式执行中（决定「执行中 · N.Ns」是否在场） */
  streaming: boolean
  /** 压缩中（文案切「压缩中」） */
  compressing?: boolean
  /** 耗时锚点 */
  streamAnchorMs?: number | null
  /** 等待用户交互（审批/选择卡在场，不显示执行中） */
  pendingInteraction: boolean
}


/**
 * 消息底部状态行：执行中指示 + 任务现场入口 + 一条现场预览。
 *
 * 无 taskId 时只渲染执行中指示（与旧 `ToolTailActivity` 行为等价，不做死入口）。
 */
export const TaskSceneStatusRow: FC<TaskSceneStatusRowProps> = ({
  taskId,
  title,
  sessionId,
  streaming,
  compressing,
  streamAnchorMs,
  pendingInteraction,
}) => {
  const isMobile = useIsMobileViewport()
  // 任务坐标兜底：会话主标签往往没有单一 taskId（只有会话级 pipelineIds），
  // 此时用消息流的真实数据面 activePipelineId（任务=管道，同源同值）。
  // 不兜底会导致入口整块不渲染（2026-11-20 真实数据实测：data-task-id=""）。
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)
  const effectiveTaskId = (typeof taskId === 'string' ? taskId.trim() : '') || (activePipelineId ?? '')
  const messages = usePipelineMessageStore((s) =>
    effectiveTaskId ? (s.messagesByPipeline[effectiveTaskId] ?? EMPTY_MESSAGES) : EMPTY_MESSAGES,
  )

  const preview = useMemo(() => {
    if (!effectiveTaskId) return undefined
    const activities = extractSceneActivities(messages)
    if (activities.length === 0) return undefined
    const instances = buildSceneInstances(activities, {
      pipelineRunning: true,
      isScreenshot: isScreenshotActivity,
    })
    const live = liveInstances(instances)
    const changes = buildFileChanges(activities)
    return buildScenePreviewText(
      live.length,
      live[live.length - 1]?.current,
      changes.length,
      changes[changes.length - 1]?.path,
    )
  }, [effectiveTaskId, messages])

  const hasTask = effectiveTaskId !== ''
  const showStreaming = streaming && !pendingInteraction

  // 既无执行中指示、也无任务入口 → 整行不渲染（不给空行占位）
  if (!showStreaming && !hasTask) return null

  return (
    <div
      className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1"
      data-testid="task-scene-status-row"
      data-task-id={taskId ?? ''}
    >
      {showStreaming && (
        <StreamActivityIndicator
          testId="tool-tail-activity"
          label={compressing ? '压缩中' : '执行中'}
          anchorMs={streamAnchorMs}
        />
      )}

      {hasTask && (
        <button
          type="button"
          onClick={() => void openTaskScene(effectiveTaskId, title, sessionId)}
          className="border-border bg-background/90 text-muted-foreground hover:bg-muted hover:text-foreground flex h-6 flex-shrink-0 items-center gap-1 rounded-full border px-2 text-[11px] shadow-sm transition-colors"
          title="打开任务现场（实例、产物文件、活动历史）"
          aria-label="打开任务现场"
          data-testid="scene-status-entry"
        >
          <Zap className="h-icon-xs w-icon-xs shrink-0" />
          {!isMobile && '任务现场'}
        </button>
      )}

      {hasTask && preview && !isMobile && (
        <button
          type="button"
          onClick={() => void openTaskScene(effectiveTaskId, title, sessionId)}
          className={cn(
            'text-muted-foreground/70 hover:text-foreground min-w-0 max-w-[60%] truncate text-[11px] transition-colors',
          )}
          title={`${preview}（点击进入任务现场）`}
          data-testid="scene-status-preview"
        >
          {preview}
        </button>
      )}
    </div>
  )
}

export default TaskSceneStatusRow
