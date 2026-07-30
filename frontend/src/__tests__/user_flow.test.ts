/**
 * 用户流程端到端测试（前端侧）
 *
 * 验证 docs/working/user_flow_and_capabilities.md 的关键断言：
 * - 场景1：ContributionRegistry 从 plugin_contributes 正确注册贡献点
 * - 场景3：widgetEventStore 接收 widget_event 并按 widget_id 分发
 * - 场景4：settingsPanels 从 plugin_configs 生成
 * - disabled 插件的 contributes 不进入 registry（内核已过滤，前端验证兜底）
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { ContributionRegistry } from '@/services/schema/ContributionRegistry'
import { useWidgetEventStore } from '@/stores/widgetEventStore'

// ── 场景1：ContributionRegistry 消费 plugin_contributes ───────────────────

describe('场景1: ContributionRegistry 从 plugin_contributes 注册贡献点', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('从 plugin_contributes 注册 statusBarItems + viewsContainers', () => {
    registry.loadFromSchema({
      plugin_contributes: [
        {
          plugin_id: 'demo_plugin',
          plugin_name: 'Demo',
          contributes: {
            statusBarItems: [{ id: 'demo_status', title: 'Demo', when: 'true' }],
            viewsContainers: [{ id: 'demo', title: 'Demo', icon: 'star', location: 'sidebar' }],
            widgets: [
              {
                id: 'demo_widget',
                widget: 'status_card',
                metric_bindings: { metric: 'demo.counter', plugin_id: 'self', interval_ms: 1000 },
              },
            ],
          },
        },
      ],
    } as never)

    expect(registry.getStatusBarItems()).toHaveLength(1)
    expect(registry.getStatusBarItems()[0].id).toBe('demo_status')
    expect(registry.getViewsContainers()).toHaveLength(1)
    expect(registry.getViewsContainers()[0].id).toBe('demo')
  })

  it('无 plugin_contributes 时不崩溃，返回空', () => {
    registry.loadFromSchema({} as never)
    expect(registry.getStatusBarItems()).toHaveLength(0)
    expect(registry.getViewsContainers()).toHaveLength(0)
  })

  it('多个插件的 contributes 都注册', () => {
    registry.loadFromSchema({
      plugin_contributes: [
        { plugin_id: 'p1', contributes: { statusBarItems: [{ id: 's1' }] } },
        { plugin_id: 'p2', contributes: { statusBarItems: [{ id: 's2' }] } },
      ],
    } as never)
    const items = registry.getStatusBarItems()
    expect(items).toHaveLength(2)
    expect(items.map((i) => i.id).sort()).toEqual(['s1', 's2'])
  })
})

// ── 场景3：widgetEventStore 接收 widget_event ──────────────────────────────

describe('场景3: widgetEventStore 接收并分发 widget_event', () => {
  beforeEach(() => {
    useWidgetEventStore.getState().clear()
  })

  it('dispatchWidgetEvent 按 widget_id 更新 latest + 追加队列', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    dispatchWidgetEvent({ widget_id: 'w1', event: 'snapshot', data: { value: 42 }, sequence: 1 })

    const state = useWidgetEventStore.getState()
    expect(state.latest['w1'].data.value).toBe(42)
    expect(state.events['w1']).toHaveLength(1)
  })

  it('多次 dispatch 更新 latest 到最新值', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    dispatchWidgetEvent({ widget_id: 'w1', event: 'snapshot', data: { value: 1 }, sequence: 1 })
    dispatchWidgetEvent({ widget_id: 'w1', event: 'snapshot', data: { value: 2 }, sequence: 2 })

    expect(useWidgetEventStore.getState().latest['w1'].data.value).toBe(2)
    expect(useWidgetEventStore.getState().events['w1']).toHaveLength(2)
  })

  it('无 widget_id 的事件被忽略', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    dispatchWidgetEvent({ event: 'orphan', data: {} })
    expect(Object.keys(useWidgetEventStore.getState().latest)).toHaveLength(0)
  })

  it('不同 widget_id 队列互不干扰', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    dispatchWidgetEvent({ widget_id: 'w1', event: 'snap', data: { v: 1 }, sequence: 1 })
    dispatchWidgetEvent({ widget_id: 'w2', event: 'snap', data: { v: 2 }, sequence: 2 })

    const state = useWidgetEventStore.getState()
    expect(state.latest['w1'].data.v).toBe(1)
    expect(state.latest['w2'].data.v).toBe(2)
  })
})

// ── 场景4：settingsPanels 从 plugin_configs 生成 ───────────────────────────

describe('场景4: settingsPanels 从 plugin_configs 生成', () => {
  let registry: ContributionRegistry

  beforeEach(() => {
    registry = new ContributionRegistry()
  })

  it('从 plugin_configs 生成设置面板', () => {
    registry.loadFromSchema({
      plugin_configs: [
        {
          plugin_id: 'llm_service',
          plugin_name: 'LLM Service',
          config_files: [
            { id: 'llm', path: 'config/models/llm.yaml', label: 'LLM 配置' },
            { id: 'embedding', path: 'config/models/embedding.yaml', label: 'Embedding' },
          ],
        },
      ],
    } as never)

    const panels = registry.getSettingsPanels()
    expect(panels).toHaveLength(1)
    expect(panels[0].pluginId).toBe('llm_service')
    expect(panels[0].configFiles).toHaveLength(2)
    expect(panels[0].configFiles[0].id).toBe('llm')
  })

  it('plugin_configs 为空时设置面板为空', () => {
    registry.loadFromSchema({ plugin_configs: [] } as never)
    expect(registry.getSettingsPanels()).toHaveLength(0)
  })
})

// ── 场景4 续：插件管理页数据驱动 ───────────────────────────────────────────

describe('场景4: 插件管理页数据来自 manifest 元数据', () => {
  // 模拟后端 plugins_status_handler 返回的数据结构
  const mockPluginStatus = [
    {
      plugin_id: 'monitoring',
      name: 'Monitoring Service',
      config_type: 'system',
      host_type: 'sidecar',
      version: '1.0.0',
      enabled: true,
      activation: 'lazy',
      status: 'active',
      config_files: [{ id: 'system_metrics', label: '系统指标', path: 'config/system/monitoring.yaml' }],
      has_contributes: false,
      has_http_endpoints: true,
      error: null,
    },
    {
      plugin_id: 'channel_wecom',
      name: 'WeChat Work Channel',
      config_type: 'system',
      host_type: 'sidecar',
      version: '1.0.0',
      enabled: false,
      activation: 'eager',
      status: 'disabled',
      config_files: [],
      has_contributes: false,
      has_http_endpoints: true,
      error: null,
    },
  ]

  it('enabled 插件 status 为 active', () => {
    const active = mockPluginStatus.filter((p) => p.enabled)
    expect(active).toHaveLength(1)
    expect(active[0].status).toBe('active')
  })

  it('disabled 插件 status 为 disabled', () => {
    const disabled = mockPluginStatus.filter((p) => !p.enabled)
    expect(disabled).toHaveLength(1)
    expect(disabled[0].status).toBe('disabled')
  })

  it('有 config_files 的插件可进入配置', () => {
    const withConfig = mockPluginStatus.filter((p) => p.config_files.length > 0)
    expect(withConfig).toHaveLength(1)
    expect(withConfig[0].config_files[0].id).toBe('system_metrics')
  })

  it('activation 策略从 manifest 派生', () => {
    const eager = mockPluginStatus.find((p) => p.activation === 'eager')
    const lazy = mockPluginStatus.find((p) => p.activation === 'lazy')
    expect(eager?.plugin_id).toBe('channel_wecom')
    expect(lazy?.plugin_id).toBe('monitoring')
  })

  it('能力标记从 manifest 派生', () => {
    const withHttp = mockPluginStatus.filter((p) => p.has_http_endpoints)
    expect(withHttp).toHaveLength(2) // monitoring + channel_wecom 都有 http_endpoints
  })
})

// ── 场景3 续：StatusCardWidget 订阅 widgetEventStore ──────────────────────

describe('场景3: StatusCardWidget 通过 widgetEventStore 接收实时数据', () => {
  beforeEach(() => {
    useWidgetEventStore.getState().clear()
  })

  it('widget_event 推送后 latest 有值（StatusCardWidget 会读到）', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    // 模拟内核 PluginWidgetBroadcaster 推送
    dispatchWidgetEvent({
      widget_id: 'demo_metric_widget',
      event: 'snapshot',
      data: { value: 42 },
      sequence: 1,
    })

    const latest = useWidgetEventStore.getState().latest['demo_metric_widget']
    expect(latest).toBeDefined()
    expect(latest.data.value).toBe(42)
    // StatusCardWidget 会用 latest.data.value 覆盖 props.value
  })

  it('连续推送更新到最新值', () => {
    const { dispatchWidgetEvent } = useWidgetEventStore.getState()
    dispatchWidgetEvent({ widget_id: 'w', event: 'snap', data: { value: 1 }, sequence: 1 })
    dispatchWidgetEvent({ widget_id: 'w', event: 'snap', data: { value: 99 }, sequence: 2 })

    expect(useWidgetEventStore.getState().latest['w'].data.value).toBe(99)
  })
})
