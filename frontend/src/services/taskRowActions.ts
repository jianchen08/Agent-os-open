/**
 * 任务行操作编排（暂停/恢复）：行级 pending 走 store 级 actingTaskIds
 * （#11 统一暴露，行按钮同源消费），失败 toast 落在操作点（不静默不装成功）。
 *
 * 独立成 services 件：widgets 目录禁 import '@/stores'（ADR 2026-10-10 widget
 * Action Bus，eslint local-rules/no-widget-store-imports）——面板行内编排经本件
 * 转介；同时为 PipelineManagerWidget（巨型冻结件）行数收缩的搬移位，纯行为
 * 搬移零变化，widget 测试经组件路径全量回归。
 */
import { toast } from '@/components/ui/sonner'
import { invalidateLongTermTasks } from '@/hooks/queries/useLongTermTasksQuery'
import { pauseTask, resumeTask } from '@/services/api/tasks'
import { useLongTermTaskStore } from '@/stores/longTermTaskStore'

/** 暂停/恢复任务行：await 窗口置 actingTaskIds（行按钮转圈禁用），成功刷新任务
 *  列表（query 化：invalidate 由活跃 useLongTermTasksQuery 订阅自动重拉），失败
 *  toast 带后端描述。 */
export async function runTaskRowAction(taskId: string, action: 'pause' | 'resume'): Promise<void> {
  const { setTaskActing } = useLongTermTaskStore.getState()
  setTaskActing(taskId, true)
  try {
    await (action === 'pause' ? pauseTask(taskId) : resumeTask(taskId))
    invalidateLongTermTasks()
  } catch (e) {
    toast.error(action === 'pause' ? '暂停任务失败' : '恢复任务失败', {
      description: (e as { message?: string })?.message ?? '请稍后重试',
    })
  } finally {
    setTaskActing(taskId, false)
  }
}
