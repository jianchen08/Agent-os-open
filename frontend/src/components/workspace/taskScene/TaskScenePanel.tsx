/**
 * 任务现场面板（工作区页签容器，按 taskId 聚合运行现场）
 *
 * **视图定位（本轮重构的核心）**：现场面板回答的是「现在有什么活着在干什么、
 * 产出了什么」，不是「把全部工具调用按时间列一遍」。因此默认视图自顶向下是：
 *
 * 1. 运行中实例看板（`SceneInstanceBoard`）——活着的通道实例卡片
 * 2. 产物看板（`TaskSceneArtifacts`）——变更清单 / 截图 / 文件树
 * 3. 活动历史（`SceneHistory`）——默认折叠的复盘区，全量调用流水退到这里
 *
 * 数据面零新增端点：任务 = 管道，`pipelineMessageStore.messagesByPipeline[taskId]`
 * 里就是该任务的全部工具调用（持久化主干）。本组件只做组装。
 *
 * 页签注册：`registerWidgets` 的 `task_scene`（moduleId `__panel_task_scene__`）
 */

import { useMemo, useState } from 'react'
import { FolderTree } from '@/assets/icons'
import { openWorkspaceTreeTab } from '@/components/schema/widgets/workspaceTreeTab'
import { cn } from '@/lib/utils'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { openDeclaredPluginPage } from '@/services/workspacePanelOpener'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { extractSceneActivities } from './sceneActivities'
import { SceneHistory } from './SceneHistory'
import { isScreenshotActivity } from './sceneImageSource'
import { SceneInstanceBoard } from './SceneInstanceBoard'
import { SceneInstanceDetail } from './SceneInstanceDetail'
import { buildSceneInstances, liveInstances } from './sceneInstances'
import { resolveInstanceTarget } from './sceneInstanceTargets'
import { TaskSceneArtifacts } from './TaskSceneArtifacts'
import type { SceneInstance } from './sceneInstances'
import type { HasPluginPage } from './sceneInstanceTargets'
import type { Message } from '@/types/models'
import type { FC } from 'react'

/** 页面声明存在性：走活注册表（插件禁用/改名即 false，卡片据此不渲染点击态） */
const hasPluginPage: HasPluginPage = (pluginId, pageId) =>
  contributionRegistry.getPluginPages(pluginId).some((page) => page.id === pageId)


export interface TaskScenePanelProps {
  /** 任务 ID（= 管道 ID） */
  taskId?: string
  /** 任务名（页签标题同源，头部展示用；缺省显示短 id） */
  title?: string
  /** 归属会话 ID（打开文件树页签时透传，保持「只看当前会话」过滤一致） */
  sessionId?: string
  panel?: string
  dataSource?: string
  [key: string]: unknown
}

export const TaskScenePanel: FC<TaskScenePanelProps> = ({ taskId, title, sessionId }) => {
  const messages = usePipelineMessageStore((s) =>
    taskId ? (s.messagesByPipeline[taskId] ?? EMPTY_MESSAGES) : EMPTY_MESSAGES,
  )
  const pipelineStatus = usePipelineMessageStore((s) =>
    taskId ? s.pipelines[taskId]?.status : undefined,
  )
  const loading = usePipelineMessageStore((s) =>
    taskId ? s.initialLoadingByPipeline[taskId] : undefined,
  )

  /** 面板内运行窗口的实例键（null = 未展开） */
  const [openInstanceKey, setOpenInstanceKey] = useState<string | null>(null)

  const activities = useMemo(() => extractSceneActivities(messages), [messages])

  // 实例派生：存活 = 有在途调用 且 管道未结束（管道结束后的在途状态是遗留，不冒充运行中）
  const instances = useMemo(
    () =>
      buildSceneInstances(activities, {
        pipelineRunning: pipelineStatus === 'running',
        isScreenshot: isScreenshotActivity,
      }),
    [activities, pipelineStatus],
  )
  const live = useMemo(() => liveInstances(instances), [instances])

  /**
   * 实例卡点击路由（用户裁定：实例要能点开）：
   * - `plugin_page` → 打开声明式插件页面（工作区侧边栏页签，不弹新窗口）；
   * - `inline` → 面板内运行窗口（终端输出 / 设备活动详情）；
   * - `unavailable` 不会走到这里（卡片本身不渲染点击态，原因显示在卡内）。
   */
  const openInstance = (instance: SceneInstance): void => {
    const target = resolveInstanceTarget(instance.category, hasPluginPage)
    if (target.kind === 'plugin_page') {
      openDeclaredPluginPage({ pluginId: target.pluginId, pageId: target.pageId })
      return
    }
    if (target.kind === 'inline') {
      setOpenInstanceKey((prev) => (prev === instance.key ? null : instance.key))
    }
  }

  /** 当前展开的运行窗口实例（按 key 回查：实例结束消失时自然不渲染） */
  const detailInstance = openInstanceKey
    ? instances.find((instance) => instance.key === openInstanceKey)
    : undefined


  if (!taskId) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center p-4 text-sm">
        缺少任务坐标（taskId），无法展示现场
      </div>
    )
  }

  // 标题去重：拿不到任务名时只显示 taskId 一次（头部曾有「{title || taskId} + taskId」两段，
  // 无标题时同一标识渲染两遍）
  const hasDistinctTitle = !!title && title !== taskId

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="task-scene-panel" data-task-id={taskId}>
      {/* 头部：任务身份 + 状态 + 实例计数 + 跳文件树 */}
      <div className="border-border flex flex-shrink-0 flex-wrap items-center gap-2 border-b px-3 py-2">
        <span className="text-foreground min-w-0 max-w-[320px] truncate text-sm font-medium">
          {hasDistinctTitle ? title : taskId}
        </span>
        {hasDistinctTitle && (
          <span className="text-muted-foreground font-mono text-[11px]">{taskId}</span>
        )}
        {pipelineStatus && (
          <span
            className={cn(
              'rounded px-1.5 py-0.5 text-[11px]',
              pipelineStatus === 'running'
                ? 'text-status-running bg-status-running/10'
                : pipelineStatus === 'error'
                  ? 'text-status-error bg-status-error/10'
                  : 'text-muted-foreground bg-muted/40',
            )}
            data-testid="task-scene-status"
          >
            {pipelineStatus === 'running'
              ? '运行中'
              : pipelineStatus === 'error'
                ? '出错'
                : pipelineStatus === 'completed'
                  ? '已完成'
                  : '空闲'}
          </span>
        )}
        <span className="text-muted-foreground/70 text-[11px]" data-testid="task-scene-live-badge">
          {live.length > 0 ? `${live.length} 个实例运行中` : '无运行实例'}
        </span>
        <button
          type="button"
          onClick={() => openWorkspaceTreeTab(taskId, title || taskId, sessionId)}
          className="text-muted-foreground hover:text-foreground border-border ml-auto flex items-center gap-1 rounded border px-2 py-0.5 text-[11px] transition-colors"
          title="按目录查看该任务的产物文件"
          data-testid="task-scene-open-file-tree"
        >
          <FolderTree className="h-icon-xs w-icon-xs" />
          文件树
        </button>
      </div>

      {/* 区1：运行中实例（默认视图的主角；卡片可点开路视图，见 openInstance） */}
      <SceneInstanceBoard
        instances={instances}
        {...(pipelineStatus ? { pipelineStatus } : {})}
        onOpenInstance={openInstance}
        hasPluginPage={hasPluginPage}
      />

      {/* 区1.5：实例运行窗口（卡片点开后面板内展开，不新开窗口/页签） */}
      {detailInstance && (
        <SceneInstanceDetail instance={detailInstance} onClose={() => setOpenInstanceKey(null)} />
      )}


      {/* 区2：产物看板（按内容自然堆叠，不 flex-1 撑满——否则内容少时会在
          变更清单与产物画面之间撑出几百 px 死空白） */}
      <div className="flex min-h-0 flex-col">
        {loading && activities.length === 0 ? (
          <div className="text-muted-foreground p-4 text-center text-xs">现场加载中…</div>
        ) : (
          <TaskSceneArtifacts taskId={taskId} activities={activities} />
        )}
      </div>

      {/* 区3：活动历史（默认折叠） */}
      <SceneHistory activities={activities} instances={instances} />
    </div>
  )
}

/** 空数组常量（zustand 选择器返回稳定引用，避免每次渲染新建空数组触发重渲染） */
const EMPTY_MESSAGES: readonly Message[] = []
