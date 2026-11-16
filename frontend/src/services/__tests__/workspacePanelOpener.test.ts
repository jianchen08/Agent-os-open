/**
 * workspacePanelOpener 测试
 *
 * 核验工作区面板打开器：
 * - 静态 props（插件 views 条目声明的 widget 配置）透传到 WorkspaceTab
 * - 重复打开同 id 只激活不重复建 tab
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { openWorkspacePanel, openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { slotRegistry } from '@/services/slots/slotRegistry'
import { loadSchemaIntoRegistries, registerWidgetStub } from '@/test/schemaLoader'

/** 每用例前重置 store，避免跨用例残留（widget 注册面=生产 main.tsx 启动同款） */
function resetStore() {
  useLayoutModeStore.setState({
    workspaceTabs: [],
    activeTabId: null,
    workspaceCollapsed: false,
  })
  useNotificationStore.setState({ notifications: [], activeBlockingNotification: null })
  slotRegistry.clear()
  widgetRegistry.clear()
  initializeWidgets()
  // 合成 widget 名补注册桩（槽装载期「声明↔注册一致性」终验需要承载 widget 存在）
  registerWidgetStub('monitoring_panel')
  registerWidgetStub('dsh_overview_card')
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

describe('openWorkspacePanelByPath — 解析失败显式报错（P0-1）', () => {
  beforeEach(resetStore)

  it('空注册表 + 未知路径：返回 false 并发出 error 通知，不建页签（不静默回首页）', () => {
    const opened = openWorkspacePanelByPath('/nonexistent')
    expect(opened).toBe(false)
    const { notifications, workspaceTabs } = {
      notifications: useNotificationStore.getState().notifications,
      workspaceTabs: useLayoutModeStore.getState().workspaceTabs,
    }
    const errors = notifications.filter((n) => n.category === 'error')
    expect(errors.length).toBeGreaterThanOrEqual(1)
    expect(errors.some((n) => n.message.includes('/nonexistent'))).toBe(true)
    expect(workspaceTabs).toHaveLength(0)
  })

  it('注册表有声明但请求另一未声明路径：同样显式报错（非空注册表仍报错）', async () => {
    await loadSchemaIntoRegistries({
      plugin_contributes: [
        {
          plugin_id: 'monitoring',
          plugin_name: 'Monitoring',
          contributes: {
            pages: [
              {
                id: 'monitoring',
                title: '监控',
                space: 'workspace',
                slot: 'activity-bar',
                path: '/monitoring',
                widget: 'monitoring_panel',
              },
            ],
          },
        },
      ],
      plugin_configs: [],
    })
    expect(openWorkspacePanelByPath('/cost')).toBe(false)
    const errors = useNotificationStore
      .getState()
      .notifications.filter((n) => n.category === 'error')
    expect(errors.some((n) => n.message.includes('/cost'))).toBe(true)
  })

  it('命中声明时不报错（错误路径只在解析失败时发出）', async () => {
    await loadSchemaIntoRegistries({
      plugin_contributes: [
        {
          plugin_id: 'monitoring',
          plugin_name: 'Monitoring',
          contributes: {
            pages: [
              {
                id: 'monitoring',
                title: '监控',
                space: 'workspace',
                slot: 'activity-bar',
                path: '/monitoring',
                widget: 'monitoring_panel',
              },
            ],
          },
        },
      ],
      plugin_configs: [],
    })
    expect(openWorkspacePanelByPath('/monitoring')).toBe(true)
    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })
})

describe('openWorkspacePanelByPath — 插件页面 props 直达', () => {
  it('插件页面的 props 透传给面板', async () => {
    resetStore()
    const page = {
      id: 'dsh.overview',
      title: 'DSH 状态',
      path: '/dsh',
      space: 'workspace',
      slot: 'activity-bar',
      widget: 'status_card',
      props: { metrics: [{ title: '插件', value: '3' }] },
    }
    // 声明经两表装载（C4：workspace.tab 槽条目承载 props 直达面）
    await loadSchemaIntoRegistries({
      plugin_contributes: [
        { plugin_id: 'dsh_adapter', plugin_name: 'DSH Adapter', contributes: { pages: [page] } },
      ],
      plugin_configs: [],
    })
    const opened = openWorkspacePanelByPath('/dsh')
    expect(opened).toBe(true)
    const tab = useLayoutModeStore.getState().workspaceTabs[0]
    expect(tab.component).toBe('status_card')
    expect(tab.props).toEqual(page.props)
    vi.restoreAllMocks()
  })
})
