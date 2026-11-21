/**
 * 运行中实例看板（现场面板主视图）
 *
 * 只显示**活着的**实例卡片：正在跑的浏览器会话、正在操作的桌面/软件、正在执行的
 * 终端。结束的实例不留在看板上（否则又堆成流水），其活动进「活动历史」区、
 * 产物仍在产物区（ZCode 用「已结束 N」汇总行处理同一问题）。
 *
 * 实例与存活判定见 `sceneInstances.ts`——由工具调用生命周期推断，不是进程监控；
 * 空态文案如实说明这一点，不假装"没有实例 = 一切正常"。
 *
 * 计时：仅在看板有活实例时每秒 tick（DSH `JobListAction` 同口径，不空转）。
 *
 * **点击路由**（用户裁定 2026-11-20「实例应该能够点击打开」）：卡片目标由
 * `sceneInstanceTargets.resolveInstanceTarget` 按通道解析——终端/桌面/软件/手机 →
 * 面板内运行窗口（`onOpenInstance`，面板挂载详情），浏览器 → 声明式侧边页签
 * （`browser_tool/browser_live`）。解析为 `unavailable` 时**不渲染点击态**并在卡内
 * 直接显示原因（用户要求：没有可用目标时不要做成死链接）。
 */

import { useEffect, useState } from 'react'
import { Circle, CircleDot, ExternalLink, Loader2 } from '@/assets/icons'
import { cn } from '@/lib/utils'
import { formatDuration } from '@/types/activity'
import { instanceStateOf, liveInstances } from './sceneInstances'
import { isActionableTarget, resolveInstanceTarget } from './sceneInstanceTargets'
import { formatStartTime, sceneCategoryIcon } from './sceneRowHelpers'
import { SceneThumbnail } from './SceneThumbnail'
import type { SceneInstance } from './sceneInstances'
import type { HasPluginPage, SceneInstanceTarget } from './sceneInstanceTargets'
import type { FC } from 'react'

/** 已运行时长（毫秒；起点缺失/非法 → undefined，展示 '--' 不造假时长） */
function elapsedMs(instance: SceneInstance, nowMs: number): number | undefined {
  if (!instance.startedAt) return undefined
  const started = new Date(instance.startedAt).getTime()
  if (Number.isNaN(started)) return undefined
  return Math.max(0, nowMs - started)
}

export interface SceneInstanceBoardProps {
  /** 全部实例（组件内部只取活的） */
  instances: readonly SceneInstance[]
  /** 管道状态（空态文案区分「已结束」与「还没有活动」） */
  pipelineStatus?: string
  /** 打开实例运行视图（plugin_page 由面板开侧边页签；inline 由面板挂运行窗口） */
  onOpenInstance?: (instance: SceneInstance) => void
  /** 插件页面声明存在性查询（缺省不校验：页面层自己报错，不静默） */
  hasPluginPage?: HasPluginPage
}

/** 运行中实例看板 */
export const SceneInstanceBoard: FC<SceneInstanceBoardProps> = ({
  instances,
  pipelineStatus,
  onOpenInstance,
  hasPluginPage,
}) => {
  const live = liveInstances(instances)
  const [nowMs, setNowMs] = useState(() => Date.now())

  // 有活实例才走时钟；无活实例不空转（DSH jobs 同口径）
  useEffect(() => {
    if (live.length === 0) return
    setNowMs(Date.now())
    const timer = setInterval(() => setNowMs(Date.now()), 1_000)
    return () => clearInterval(timer)
  }, [live.length])

  return (
    <section
      className="border-border flex flex-shrink-0 flex-col border-b"
      aria-label="运行中实例"
      data-testid="task-scene-instances"
      data-live-count={live.length}
    >
      <div className="flex flex-shrink-0 items-center gap-2 px-3 py-1.5">
        <span className="text-foreground text-xs font-medium">运行中实例</span>
        <span className="text-muted-foreground/70 text-[11px]" data-testid="task-scene-live-count">
          {live.length > 0 ? `${live.length} 个在跑` : '无'}
        </span>
        <span
          className="text-muted-foreground/60 ml-auto text-[10px]"
          title="实例由工具调用生命周期推断，非进程监控"
        >
          按调用推断
        </span>
      </div>

      {live.length === 0 ? (
        <div
          className="text-muted-foreground px-3 pb-2 text-[11px]"
          data-testid="scene-instance-empty"
        >
          {pipelineStatus === 'running'
            ? '任务在跑，但当前没有进行中的通道调用'
            : '当前没有正在运行的实例'}
        </div>
      ) : (
        <div
          className="grid grid-cols-[repeat(auto-fit,minmax(min(220px,100%),1fr))] gap-1.5 px-2.5 pb-2"
          data-testid="scene-instance-grid"
        >
          {live.map((instance) => (
            <SceneInstanceCard
              key={instance.key}
              instance={instance}
              nowMs={nowMs}
              target={resolveInstanceTarget(instance.category, hasPluginPage)}
              {...(onOpenInstance ? { onOpen: () => onOpenInstance(instance) } : {})}
            />
          ))}
        </div>
      )}
    </section>
  )
}

export interface SceneInstanceCardProps {
  instance: SceneInstance
  /** 当前时刻（计时基准，由看板统一注入，卡片自身不走时钟） */
  nowMs: number
  /** 点击目标（缺省按通道现算；看板注入以复用同一次声明查询） */
  target?: SceneInstanceTarget
  /** 打开运行视图（unavailable / 未注入时不产生点击态） */
  onOpen?: () => void
}

/** 单个实例卡片（可点击打开运行视图；无可用目标时不可点并明示原因） */
export const SceneInstanceCard: FC<SceneInstanceCardProps> = ({
  instance,
  nowMs,
  target,
  onOpen,
}) => {
  const Icon = sceneCategoryIcon(instance.category)
  const elapsed = elapsedMs(instance, nowMs)
  const shot = instance.screenshots[instance.screenshots.length - 1]
  const resolved = target ?? resolveInstanceTarget(instance.category)
  const actionable = isActionableTarget(resolved) && !!onOpen
  const targetLabel = resolved.kind === 'plugin_page' ? resolved.label : '运行窗口'
  const unavailableReason =
    resolved.kind === 'unavailable' ? resolved.reason : '这个实例没有可打开的运行视图'

  const content = (
    <>
      <div className="flex items-center gap-1.5">
        <Icon className="text-muted-foreground h-icon-sm w-icon-sm flex-shrink-0" />
        <span
          className="text-foreground min-w-0 flex-1 truncate text-[11px] font-medium"
          title={instance.focus ? `${instance.label} · ${instance.focus}` : instance.label}
        >
          {instance.label}
          {instance.focus && (
            <span className="text-muted-foreground/70 font-normal"> · {instance.focus}</span>
          )}
        </span>
        <span
          className="text-status-running flex flex-shrink-0 items-center gap-1 text-[10px]"
          data-testid="scene-instance-state"
        >
          <CircleDot className="h-icon-xs w-icon-xs" />
          运行中
        </span>
        <Loader2 className="text-status-running h-icon-xs w-icon-xs flex-shrink-0 animate-spin" />
      </div>

      <div
        className="text-muted-foreground mt-0.5 truncate text-[11px]"
        title={instance.current}
        data-testid="scene-instance-current"
      >
        {instance.current}
      </div>

      <div className="mt-0.5 flex items-center gap-2 text-[10px]">
        <span
          className="text-muted-foreground/70 flex items-center gap-0.5 font-mono"
          data-testid="scene-instance-elapsed"
          data-elapsed-ms={elapsed ?? ''}
        >
          <Circle className="h-icon-xs w-icon-xs" />
          {elapsed !== undefined ? formatDuration(elapsed) : '--'}
        </span>
        {instance.startedAt && (
          <span className="text-muted-foreground/60 font-mono">
            起 {formatStartTime(instance.startedAt)}
          </span>
        )}
        <span className="text-muted-foreground/60">
          {instance.activities.length} 次 · {instance.inflightCount} 在途
        </span>
        {shot && (
          <span className="ml-auto flex items-center gap-1">
            <SceneThumbnail activity={shot} containerTaskId={shot.containerTaskId} />
            {instance.screenshots.length > 1 && (
              <span className="text-muted-foreground/60">{instance.screenshots.length} 图</span>
            )}
          </span>
        )}

        {/* 打开目标：可点 → 目标名 + 外链图标；不可用 → 原因文本（不做死链接） */}
        {actionable ? (
          <span
            className="text-primary flex flex-shrink-0 items-center gap-0.5"
            data-testid="scene-instance-open-hint"
          >
            {targetLabel}
            <ExternalLink className="h-icon-xs w-icon-xs" />
          </span>
        ) : (
          <span
            className="text-muted-foreground/60 ml-auto min-w-0 truncate"
            title={unavailableReason}
            data-testid="scene-instance-open-unavailable"
          >
            {unavailableReason}
          </span>
        )}
      </div>
    </>
  )

  const commonProps = {
    'data-testid': 'scene-instance-card',
    'data-instance-key': instance.key,
    'data-instance-category': instance.category,
    'data-instance-state': instanceStateOf(instance),
    'data-instance-target': resolved.kind,
  } as const

  if (!actionable) {
    // 不可点：无 cursor-pointer、无 hover 态，原因已在卡内显示
    return (
      <div
        className="border-border bg-muted/20 w-full min-w-0 rounded border px-2 py-1.5"
        {...commonProps}
      >
        {content}
      </div>
    )
  }

  return (
    <button
      type="button"
      onClick={() => onOpen?.()}
      className="border-border bg-muted/20 hover:border-primary/40 hover:bg-[var(--hover-overlay)] w-full min-w-0 cursor-pointer rounded border px-2 py-1.5 text-left transition-colors"
      title={
        resolved.kind === 'plugin_page'
          ? `打开${resolved.label}（工作区侧边栏）`
          : resolved.kind === 'inline'
            ? `打开${resolved.label}：${instance.label}${instance.focus ? ` · ${instance.focus}` : ''}`
            : resolved.reason
      }
      {...commonProps}
    >
      {content}
    </button>
  )
}

/** 实例类别徽标（历史区组头复用，避免组头另写一套图标逻辑） */
export const SceneInstanceCategoryBadge: FC<{
  category: SceneInstance['category']
  className?: string
}> = ({ category, className }) => {
  const Icon = sceneCategoryIcon(category)
  return <Icon className={cn('text-muted-foreground h-icon-sm w-icon-sm flex-shrink-0', className)} />
}
