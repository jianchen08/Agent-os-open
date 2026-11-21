/**
 * 聊天区右上角「任务现场」入口（与待办角标同列并排）
 *
 * 用户裁定（2026-11-17）：任务现场入口放界面右上角、与待办入口并列，不放在
 * 消息流的会话头部。本组件经 ChatFloatingLayer 的宿主槽位渲染——同锚点归组
 * 由该层统一排位，本组件不写死任何偏移。图标与任务管理页的任务现场入口一致
 * （Zap，PipelineManagerTreeParts.tsx 行内按钮同源）。
 *
 * 坐标来源：任务 = 管道（两 id 同源同值），当前会话对应任务 = 当前标签的管道
 * （子标签用其 taskId，主标签用主管道 id），与任务管理页 sceneId 解析
 * （`entry.taskId || entry.pipelineId`）同口径。
 *
 * 按需加载（本入口成立的先决条件）：现场页签只读
 * `pipelineMessageStore.messagesByPipeline[taskId]`，未加载过就是空列表——
 * 本入口在打开页签前先补齐该任务消息，避免用户点进来看到空页。
 */

import { Zap } from 'lucide-react'
import { openTaskSceneTab } from '@/components/workspace/taskScene/taskSceneTab'
import { useIsMobileViewport } from '@/hooks/useIsMobileViewport'
import { cn } from '@/lib/utils'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'

export interface TaskSceneEntryButtonProps {
  /** 当前会话对应任务的坐标（任务=管道；空 = 未定位到任务） */
  taskId?: string
  /** 任务名（现场页签标题同源；缺省显示短 id） */
  title?: string
  /** 归属会话（工作区页签「只看当前会话」过滤键） */
  sessionId?: string
}

/**
 * 打开现场页签（先补消息，再开签）
 *
 * 消息已加载（>0 条）直接开签，不发多余请求；未加载则走统一加载入口
 * loadPipelineMessages（含并发防重、游标决策与失败契约）。加载失败不阻断
 * 开签——页签自身有「加载中/空态」两态，但仍显式告知用户拉取失败，
 * 不做静默降级。
 */
// 入口按钮与其开签器是同一工作单元（按钮 onClick 唯一消费方），
// 拆件反而制造双文件跳读
// eslint-disable-next-line react-refresh/only-export-components
export async function openTaskScene(
  taskId: string,
  title?: string,
  sessionId?: string,
): Promise<void> {
  if (!taskId) return
  const store = usePipelineMessageStore.getState()
  const loaded = (store.messagesByPipeline[taskId]?.length ?? 0) > 0
  const threadId = sessionId || store.pipelineSessionMap[taskId] || ''
  if (!loaded && threadId) {
    const { ok } = await store.loadPipelineMessages(taskId, { threadId })
    if (!ok) {
      useNotificationStore.getState().addNotification({
        title: '任务现场消息拉取失败',
        message: `任务 ${taskId.slice(0, 12)} 的历史消息加载失败，现场可能是空页，可稍后重试`,
        priority: 'normal',
        category: 'alert',
        isBlocking: false,
        autoDismissMs: 6000,
        sourceLabel: '系统',
      })
    }
  }
  openTaskSceneTab(taskId, title, sessionId)
}

export function TaskSceneEntryButton({ taskId, title, sessionId }: TaskSceneEntryButtonProps) {
  const normalizedTaskId = enabledTaskId(taskId)
  const hasTask = normalizedTaskId !== ''
  // 窄视口图标化（视觉优化原则 1，P0-1）：移动顶栏横向空间紧张，胶囊文字
  // 「现场」退场，语义由 aria-label/title 承载
  const isMobile = useIsMobileViewport()
  return (
    <button
      type="button"
      disabled={!hasTask}
      title={
        hasTask
          ? '任务现场（按时间看这次对话做了什么、产出了什么）'
          : '当前会话未定位到任务管道，暂无可打开的任务现场'
      }
      aria-label="任务现场"
      data-testid="chat-task-scene-btn"
      onClick={() => {
        if (!hasTask) return
        void openTaskScene(normalizedTaskId, title, sessionId)
      }}
      className={cn(
        'flex h-7 items-center gap-1 rounded-full border px-2.5 text-xs shadow-sm transition-colors',
        hasTask
          ? 'border-border bg-background/90 text-muted-foreground hover:bg-muted hover:text-foreground'
          : 'border-border/50 text-muted-foreground/50 cursor-not-allowed',
      )}
    >
      <Zap className="h-icon-sm w-icon-sm shrink-0" />
      {!isMobile && '现场'}
    </button>
  )
}

/** 任务坐标归一：空串/纯空白视为无任务（不建无坐标的现场页签） */
function enabledTaskId(taskId: string | undefined): string {
  return typeof taskId === 'string' ? taskId.trim() : ''
}

export default TaskSceneEntryButton
