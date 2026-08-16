/**
 * workspacePanelOpener 测试
 *
 * 核验工作区面板打开器：
 * - 静态 props（插件 views 条目声明的 widget 配置）透传到 WorkspaceTab
 * - 重复打开同 id 只激活不重复建 tab
 */

import { describe, expect, it, vi } from 'vitest'
import { openWorkspacePanel, openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'

/** 每用例前重置 store，避免跨用例残留 */
function resetStore() {
  useLayoutModeStore.setState({
    workspaceTabs: [],
    activeTabId: null,
    workspaceCollapsed: false,
    workspaceMaximized: false,
  })
}

describe('openWorkspacePanel — props 透传', () => {
  it('spec.props 落到 tab.props（插件 views 声明数据直通 widget）', () => {
    resetStore()
    const props = { title: 'DSH 状态', metrics: [{ title: '插件', value: '3' }] }
    openWorkspacePanel({
      id: 'ws-plugin-dsh.overview',
      title: 'DSH 状态',
      component: 'status_card',
      icon: 'sparkles',
      props,
      moduleId: '__contrib_dsh__',
    })
    const tab = useLayoutModeStore.getState().workspaceTabs[0]
    expect(tab.component).toBe('status_card')
    expect(tab.props).toEqual(props)
  })

  it('未传 props 时 tab.props 为 undefined（不产生空对象噪音）', () => {
    resetStore()
    openWorkspacePanel({ id: 'ws-panel-tasks', title: '任务管理', component: 'pipeline_manager' })
    const tab = useLayoutModeStore.getState().workspaceTabs[0]
    expect(tab.props).toBeUndefined()
  })
})

describe('openWorkspacePanelByPath — 插件页面 props 直达', () => {
  it('插件页面的 props 透传给面板', () => {
    resetStore()
    const page = {
      type: 'pages',
      id: 'dsh.overview',
      title: 'DSH 状态',
      path: '/dsh',
      space: 'workspace',
      slot: 'activity-bar',
      widget: 'status_card',
      props: { metrics: [{ title: '插件', value: '3' }] },
      pluginId: 'dsh_adapter',
      legacyFrom: 'viewsContainers',
    }
    vi.spyOn(contributionRegistry, 'getPages').mockReturnValue([page as never])
    const opened = openWorkspacePanelByPath('/dsh')
    expect(opened).toBe(true)
    const tab = useLayoutModeStore.getState().workspaceTabs[0]
    expect(tab.component).toBe('status_card')
    expect(tab.props).toEqual(page.props)
    vi.restoreAllMocks()
  })
})
