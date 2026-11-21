/** @feature FP-0.2.四 前端Schema（任务现场） @vision V6 可即用 @ci frontend-test */
/**
 * openTaskSceneTab 页签契约测试（任务现场批 1）。
 *
 * 判据（方案 §七 批 1-1）：从入口打开 → 工作区出现「任务现场」页签；同任务重复
 * 打开幂等激活（不重复建签、不改写归属）；不同任务各自一签。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { openTaskSceneTab, taskSceneTabId } from '@/components/workspace/taskScene/taskSceneTab'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

describe('openTaskSceneTab — 页签建立与幂等', () => {
  beforeEach(() => {
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  })

  it('首次打开：建立 task_scene 页签，携带 taskId 坐标与归属会话', () => {
    openTaskSceneTab('task-abc', '光速任务', 'sess-1')
    const tab = useLayoutModeStore.getState().workspaceTabs.find((t) => t.id === 'ws-scene-task-abc')
    expect(tab).toBeDefined()
    expect(tab?.component).toBe('task_scene')
    // title 一并透传：面板头部据此显示任务名（否则只能显示 taskId）
    expect(tab?.props).toEqual({ taskId: 'task-abc', title: '光速任务' })
    expect(tab?.title).toContain('光速任务')
    expect(tab?.ownerSessionId).toBe('sess-1')
    expect(tab?.isActive).toBe(true)
  })

  it('无 title 打开：props 只带 taskId（不塞空串 title，避免头部出现空白标题）', () => {
    openTaskSceneTab('task-notitle')
    const tab = useLayoutModeStore.getState().workspaceTabs.find((t) => t.id === 'ws-scene-task-notitle')
    expect(tab?.props).toEqual({ taskId: 'task-notitle' })
    expect(tab?.title).toBe('任务现场')
  })

  it('重复打开同一任务：只激活，不新建第二签', () => {
    openTaskSceneTab('task-dup', '任务')
    useLayoutModeStore.getState().addWorkspaceTab({
      id: 'other-tab',
      title: '其它',
      moduleId: '__panel_other__',
      isActive: false,
      isPinned: false,
    })
    openTaskSceneTab('task-dup', '任务')
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.filter((t) => t.id === taskSceneTabId('task-dup'))).toHaveLength(1)
    expect(tabs.find((t) => t.isActive)?.id).toBe(taskSceneTabId('task-dup'))
  })

  it('不同任务：各自独立页签（区分度断言）', () => {
    openTaskSceneTab('task-a', 'A')
    openTaskSceneTab('task-b', 'B')
    const ids = useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)
    expect(ids).toContain('ws-scene-task-a')
    expect(ids).toContain('ws-scene-task-b')
    expect(ids).toHaveLength(2)
  })

  it('空 taskId：不建签（无坐标的现场页无意义）', () => {
    openTaskSceneTab('', '空')
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(0)
  })
})
