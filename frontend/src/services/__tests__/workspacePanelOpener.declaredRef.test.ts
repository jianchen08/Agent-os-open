/**
 * openDeclaredPluginPage · 声明引用导航（审查总册 E6 壳层软绑定声明面化）
 *
 * 验收口径：壳层导航只持声明键（pluginId+pageId，pluginPages.generated.ts 投影），
 * 运行时经 contributionRegistry 活注册表解析——
 * - 随动：插件改页面声明（path/title/widget）后 loadFromSchema 刷新，同一声明键
 *   导航随新声明走（注册表刷新测试）；
 * - 断链双向可见：声明移除（插件禁用）/页面改名后旧键解析失败 → 显式 error
 *   通知 + false，绝不静默；构建期（tsc 判别联合 + 漂移闸）另有闸红，此处验运行时。
 * 样例声明为机制验证用合成数据。
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { PLUGIN_DECLARED_PAGES } from '@/services/schema/pluginPages.generated'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { openDeclaredPluginPage } from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { loadSchemaIntoRegistries, registerWidgetStub } from '@/test/schemaLoader'

/** 装载页声明：contributionRegistry + workspace.tab 槽同批（等价 GrowthLoop 装载序） */
async function seedMonitoringPage(page: Record<string, unknown>): Promise<void> {
  await loadSchemaIntoRegistries({
    plugin_contributes: [
      {
        plugin_id: 'monitoring',
        plugin_name: 'Monitoring',
        contributes: { pages: [page] },
      },
    ],
    plugin_configs: [],
  })
}

const BASE_PAGE = {
  id: 'monitoring',
  title: '监控',
  icon: '📊',
  space: 'workspace',
  slot: 'activity-bar',
  order: 20,
  path: '/monitoring',
  widget: 'widget_stage',
  props: { space: 'monitoring' },
}

beforeEach(async () => {
  useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null })
  useNotificationStore.setState({ notifications: [], activeBlockingNotification: null })
  // 内建 widget 注册面（生产 main.tsx 启动同款）；槽装载期终验要求承载 widget 已注册
  widgetRegistry.clear()
  initializeWidgets()
  // 合成 widget 名补注册桩（机制验证用样例声明）
  registerWidgetStub('monitoring_panel_next')
  registerWidgetStub('debug_monitoring')
  await seedMonitoringPage(BASE_PAGE)
})

describe('openDeclaredPluginPage · 声明引用导航', () => {
  it('声明命中：投影引用 → 打开对应 widget 页签（props 透传）', async () => {
    const ref = PLUGIN_DECLARED_PAGES.monitoring.monitoring
    expect(ref).toBeDefined()
    expect(openDeclaredPluginPage(ref)).toBe(true)

    const tab = useLayoutModeStore
      .getState()
      .workspaceTabs.find((t) => t.id === 'ws-plugin-monitoring')
    expect(tab).toBeDefined()
    expect(tab?.component).toBe('widget_stage')
    expect(tab?.props).toEqual({ space: 'monitoring' })
  })

  it('随动（注册表刷新）：声明改 path/title/widget 后 loadFromSchema 重载，同一声明键随新声明走', async () => {
    expect(openDeclaredPluginPage(PLUGIN_DECLARED_PAGES.monitoring.monitoring)).toBe(true)

    // 插件改声明（标题/组件/props 变更，键不变）→ schema 刷新（等价 GrowthLoop 重载）
    await seedMonitoringPage({
      ...BASE_PAGE,
      title: '监控 v2',
      widget: 'monitoring_panel_next',
      props: { space: 'monitoring-v2' },
    })

    useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null })
    expect(openDeclaredPluginPage(PLUGIN_DECLARED_PAGES.monitoring.monitoring)).toBe(true)
    const tab = useLayoutModeStore
      .getState()
      .workspaceTabs.find((t) => t.id === 'ws-plugin-monitoring')
    expect(tab?.title).toBe('监控 v2')
    expect(tab?.component).toBe('monitoring_panel_next')
    expect(tab?.props).toEqual({ space: 'monitoring-v2' })
  })

  it('页面改名（投影随动后）：新键解析新声明；旧键断链显式报错不静默', async () => {
    // 插件把页面 id monitoring → obs（投影重生成后壳层引用随动到新键）
    await seedMonitoringPage({ ...BASE_PAGE, id: 'obs', title: '观测台' })
    const newRef = { pluginId: 'monitoring', pageId: 'obs' } as typeof PLUGIN_DECLARED_PAGES.monitoring.monitoring

    expect(openDeclaredPluginPage(newRef)).toBe(true)
    const tab = useLayoutModeStore
      .getState()
      .workspaceTabs.find((t) => t.id === 'ws-plugin-obs')
    expect(tab?.title).toBe('观测台')

    // 旧键（改名前残留引用）→ 注册表无此页 → false + error 通知（断链可见）
    useNotificationStore.setState({ notifications: [], activeBlockingNotification: null })
    const staleRef = { pluginId: 'monitoring', pageId: 'monitoring' } as typeof newRef
    expect(openDeclaredPluginPage(staleRef)).toBe(false)
    expect(
      useNotificationStore
        .getState()
        .notifications.some((n) => n.category === 'error' && n.message.includes('monitoring')),
    ).toBe(true)
  })

  it('断链（插件禁用=声明移除）：解析失败 → false + error 通知，不静默不回退', async () => {
    // 用 agent_manager 夹具：通知按 title+message 指纹短窗去重（notificationStore
    // 刷屏闸），与上一用例的 monitoring 旧键报错错开指纹，断言才可判数
    await loadSchemaIntoRegistries({
      plugin_contributes: [
        {
          plugin_id: 'agent_manager',
          plugin_name: 'Agent Manager',
          contributes: {
            pages: [
              { id: 'agent_manager_agents', title: '智能体', space: 'workspace', path: '/agents', widget: 'agents_panel' },
            ],
          },
        },
      ],
      plugin_configs: [],
    })
    const ref = {
      pluginId: 'agent_manager',
      pageId: 'agent_manager_agents',
    } as typeof PLUGIN_DECLARED_PAGES.agent_manager.agent_manager_agents
    expect(openDeclaredPluginPage(ref)).toBe(true)

    // 插件禁用（声明移除）：两表同批清空（槽条目随之撤下）
    await loadSchemaIntoRegistries({ plugin_contributes: [], plugin_configs: [] })
    useLayoutModeStore.setState({ workspaceTabs: [], activeTabId: null })
    useNotificationStore.setState({ notifications: [], activeBlockingNotification: null })
    expect(openDeclaredPluginPage(ref)).toBe(false)
    const errors = useNotificationStore.getState().notifications.filter((n) => n.category === 'error')
    expect(errors).toHaveLength(1)
    expect(errors[0].message).toContain('agent_manager')
    expect(useLayoutModeStore.getState().workspaceTabs).toHaveLength(0)
  })

  it('插件命名空间隔离：裸 id 同名（跨插件）按 pluginId 作用域解析，不串台', async () => {
    await loadSchemaIntoRegistries({
      plugin_contributes: [
        {
          plugin_id: 'debug_center',
          plugin_name: 'Debug Center',
          contributes: {
            pages: [{ id: 'monitoring', title: '调试监控', space: 'debug_center', widget: 'debug_monitoring' }],
          },
        },
        { plugin_id: 'monitoring', plugin_name: 'Monitoring', contributes: { pages: [BASE_PAGE] } },
      ],
      plugin_configs: [],
    })

    expect(
      openDeclaredPluginPage({ pluginId: 'monitoring', pageId: 'monitoring' } as typeof PLUGIN_DECLARED_PAGES.monitoring.monitoring),
    ).toBe(true)
    // 命中的是 monitoring 插件自己的声明页，不是先注册的 debug_center 同名页
    const tab = useLayoutModeStore
      .getState()
      .workspaceTabs.find((t) => t.id === 'ws-plugin-monitoring')
    expect(tab?.component).toBe('widget_stage')
    expect(tab?.moduleId).toBe('__plugin_monitoring__')
  })

  it('页存在但无 path 无 widget：openPluginPage 层显式报错，返回 false 不静默', async () => {
    await seedMonitoringPage({ id: 'monitoring', title: '监控', space: 'workspace' })
    useNotificationStore.setState({ notifications: [], activeBlockingNotification: null })

    expect(openDeclaredPluginPage(PLUGIN_DECLARED_PAGES.monitoring.monitoring)).toBe(false)
    expect(
      useNotificationStore
        .getState()
        .notifications.some((n) => n.message.includes('未声明 path 或 widget')),
    ).toBe(true)
  })
})
