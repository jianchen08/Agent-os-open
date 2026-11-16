/** @feature FP-0.2.四 前端Schema（工作区树页签） @vision V6 可即用 @ci frontend-test */
/**
 * openWorkspaceTreeTab ownerSessionId 写入测试（工作区「只看当前会话」过滤键）：
 * - 第三参写入创建的页签
 * - 缺省第三参 = 无 ownerSessionId（全局页签）
 * - 已存在同 id 页签仅激活，不改写归属（幂等激活分支不变）
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { openWorkspaceTreeTab } from '@/components/schema/widgets/workspaceTreeTab'
import { useLayoutModeStore } from '@/stores/layoutModeStore'

describe('openWorkspaceTreeTab — ownerSessionId 写入', () => {
  beforeEach(() => {
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  })

  it('传第三参：创建的页签写入 ownerSessionId', () => {
    openWorkspaceTreeTab('task-1', '任务一', 'sess-1')
    const tab = useLayoutModeStore
      .getState()
      .workspaceTabs.find((t) => t.id === 'ws-tree-task-1')
    expect(tab?.ownerSessionId).toBe('sess-1')
    expect(tab?.dataSource).toBe('workspace://task-1')
    expect(tab?.isActive).toBe(true)
  })

  it('缺省第三参：无 ownerSessionId（全局页签，任何会话下可见）', () => {
    openWorkspaceTreeTab('task-2', '任务二')
    const tab = useLayoutModeStore
      .getState()
      .workspaceTabs.find((t) => t.id === 'ws-tree-task-2')
    expect(tab?.ownerSessionId).toBeUndefined()
  })

  it('已存在同 id 页签：仅激活，不重复创建也不改写归属', () => {
    openWorkspaceTreeTab('task-3', '任务三', 'sess-1')
    openWorkspaceTreeTab('task-3', '任务三')
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.filter((t) => t.id === 'ws-tree-task-3')).toHaveLength(1)
    expect(tabs[0]?.ownerSessionId).toBe('sess-1')
    expect(tabs[0]?.isActive).toBe(true)
  })
})
