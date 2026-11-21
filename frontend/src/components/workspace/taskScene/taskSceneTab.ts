/**
 * 打开「任务现场」工作区页签
 *
 * 比照 openWorkspaceTreeTab（workspaceTreeTab.ts）的幂等打开口径：已存在同 id
 * 页签则仅激活，否则按 workspaceTabs 既有契约新建。页签内容是声明注册表里的
 * `task_scene` widget（组件在本目录外的 components/workspace/taskScene/），
 * 任务坐标经 props.taskId 交给组件——组件按 taskId 拉消息面供数。
 *
 * 与文件树分工（方案 §3.1）：文件树 = 按目录看；任务现场 = 按时间/活动看。
 *
 * 依据：docs/working/9b59e3318956_工作区任务现场方案.md §3.1
 */

import { useLayoutModeStore } from '@/stores/layoutModeStore'

/** 任务现场页签 id（幂等键，<taskId> 后缀保证一任务一页签） */
export function taskSceneTabId(taskId: string): string {
  return `ws-scene-${taskId}`
}

/**
 * 打开/激活任务现场页签
 *
 * @param taskId 任务 ID（= 管道 ID，任务即管道，两 id 同源同值）
 * @param title 页签标题（缺省「任务现场」；调用方可传任务名）
 * @param ownerSessionId 归属会话（工作区「只看当前会话」过滤键；缺省 = 全局页签）
 */
export function openTaskSceneTab(taskId: string, title?: string, ownerSessionId?: string): void {
  if (!taskId) return
  const layoutStore = useLayoutModeStore.getState()
  const tabId = taskSceneTabId(taskId)
  const existingTab = layoutStore.workspaceTabs.find((t) => t.id === tabId)
  if (existingTab) {
    layoutStore.setActiveTab(tabId)
    return
  }
  layoutStore.addWorkspaceTab({
    id: tabId,
    title: title ? `现场 · ${title}` : '任务现场',
    icon: '🎬',
    moduleId: '__panel_task_scene__',
    component: 'task_scene',
    // title 一并透传：面板头部据此显示任务名（不传则只能显示 taskId）
    props: { taskId, ...(title ? { title } : {}) },
    ownerSessionId,
    isActive: true,
    isPinned: false,
  })
}
