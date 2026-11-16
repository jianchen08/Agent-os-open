/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * autoOpenModePanel —— 会话对话标签激活时的模式面板自动弹出（R92 · D-2）
 *
 * 契约：
 * - 数据源 = 共享 pipelineStates query 缓存（GET /api/v1/pipelines/state 的
 *   前端镜像，只读，不发新请求）
 * - 管道 state.mode 非空且存在 workspace/tab 等值 mode 页面声明 → 打开并激活
 *   面板页签；幂等（已开 = 激活，不重复开）
 * - 声明 autoOpen:false = 自动弹出选退（对话标签激活不再开/激活该页签；
 *   显式入口 openPluginPage 不受影响）——仅自动通道消费，声明驱动零硬编码
 * - pipelineId 空 / 缓存无该管道 / 无 mode 键 / 无匹配声明 → 不动作
 *
 * 测试策略：真实 queryClient 缓存 / ContributionRegistry / layoutModeStore，
 * 无网络依赖，不 mock。
 */

import { beforeEach, describe, expect, it } from 'vitest'
import { autoOpenModePanel } from '@/services/modePanelAutoOpen'
import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { slotRegistry } from '@/services/slots/slotRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { seedPipelineState } from '@/test/modePanelTestUtils'
import { loadSchemaIntoRegistries } from '@/test/schemaLoader'
import type { PipelineStateInfo } from '@/services/api/pipelines'

const MODE_PAGE = {
  type: 'pages' as const,
  id: 'coding_delivery',
  title: '编码交付',
  space: 'workspace' as const,
  slot: 'tab' as const,
  path: '/p/coding_delivery',
  widget: 'status_card',
  mode: 'coding',
  pluginId: 'mode_coding',
}

type PageFixture = Record<string, unknown> & { pluginId: string }

/**
 * 声明播种：按 pluginId 分组经两表装载（等价 GrowthLoop 装载序）。
 * C4 收敛后 path 直达按 workspace.tab keyed 槽解析，只喂 contributionRegistry
 * 的夹具会解析落空（设计真值 = ADR 2026-11-04-frontend-slot-and-widget-delivery）。
 */
async function seedPages(...pages: PageFixture[]): Promise<void> {
  const byPlugin = new Map<string, PageFixture[]>()
  for (const page of pages) {
    const bucket = byPlugin.get(page.pluginId) ?? []
    bucket.push(page)
    byPlugin.set(page.pluginId, bucket)
  }
  await loadSchemaIntoRegistries({
    plugin_contributes: [...byPlugin.entries()].map(([plugin_id, list]) => ({
      plugin_id,
      plugin_name: plugin_id,
      contributes: { pages: list },
    })),
    plugin_configs: [],
  })
}


describe('autoOpenModePanel', () => {
  beforeEach(() => {
    queryClient.removeQueries()
    contributionRegistry.clear()
    slotRegistry.clear()
    // 内建 widget 注册面（生产 main.tsx 启动同款）：槽装载期终验要求承载 widget 已注册
    widgetRegistry.clear()
    initializeWidgets()
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  })

  it('mode 命中声明 → 面板页签打开并激活', async () => {
    await seedPages(MODE_PAGE)
    seedPipelineState('pipe-1', { mode: 'coding' })
    autoOpenModePanel('pipe-1')
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.map((t) => t.id)).toEqual(['ws-plugin-coding_delivery'])
    expect(tabs[0]?.isActive).toBe(true)
  })

  it('幂等：已开不重复开；已开未激活 → 激活', async () => {
    await seedPages(MODE_PAGE)
    seedPipelineState('pipe-1', { mode: 'coding' })
    autoOpenModePanel('pipe-1')
    useLayoutModeStore.getState().addWorkspaceTab({
      id: 'ws-other', title: '其他', moduleId: 'm', component: 'c', isActive: true, isPinned: false,
    })
    autoOpenModePanel('pipe-1')
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.filter((t) => t.id === 'ws-plugin-coding_delivery')).toHaveLength(1)
    expect(tabs.find((t) => t.id === 'ws-plugin-coding_delivery')?.isActive).toBe(true)
    expect(tabs.find((t) => t.id === 'ws-other')?.isActive).toBe(false)
  })

  it('无 mode 键 → 不动作', async () => {
    await seedPages(MODE_PAGE)
    seedPipelineState('pipe-2', {})
    autoOpenModePanel('pipe-2')
    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
  })

  it('mode 无匹配声明 → 不动作', async () => {
    await seedPages(MODE_PAGE)
    seedPipelineState('pipe-3', { mode: 'writing' })
    autoOpenModePanel('pipe-3')
    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
  })

  it('缓存无该管道 / pipelineId 为空 → 不动作', async () => {
    await seedPages(MODE_PAGE)
    seedPipelineState('pipe-1', { mode: 'coding' })
    autoOpenModePanel('pipe-unknown')
    autoOpenModePanel(undefined)
    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
  })

  it('声明 autoOpen:false → 对话标签激活不开面板（选退，个人助理诉求）', async () => {
    await seedPages({ ...MODE_PAGE, autoOpen: false })
    seedPipelineState('pipe-1', { mode: 'coding' })
    autoOpenModePanel('pipe-1')
    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
  })

  it('autoOpen:false 页面不因已开而被自动激活', async () => {
    await seedPages({ ...MODE_PAGE, autoOpen: false })
    seedPipelineState('pipe-1', { mode: 'coding' })
    useLayoutModeStore.getState().addWorkspaceTab({
      id: 'ws-plugin-coding_delivery', title: '编码交付', moduleId: 'm', component: 'c', isActive: false, isPinned: false,
    })
    useLayoutModeStore.getState().addWorkspaceTab({
      id: 'ws-other', title: '其他', moduleId: 'm2', component: 'c2', isActive: true, isPinned: false,
    })
    autoOpenModePanel('pipe-1')
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.find((t) => t.id === 'ws-plugin-coding_delivery')?.isActive).toBe(false)
    expect(tabs.find((t) => t.id === 'ws-other')?.isActive).toBe(true)
  })

  it('同 mode 多声明：首选页选退 → 落到次选愿弹页（候选制，order 序）', async () => {
    await seedPages(
      { ...MODE_PAGE, autoOpen: false, order: 10 },
      { ...MODE_PAGE, id: 'coding_scratch', path: '/p/coding_scratch', order: 20 },
    )
    seedPipelineState('pipe-1', { mode: 'coding' })
    autoOpenModePanel('pipe-1')
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual([
      'ws-plugin-coding_scratch',
    ])
  })

  it('同 mode 多声明：全员选退 → 零动作', async () => {
    await seedPages(
      { ...MODE_PAGE, autoOpen: false, order: 10 },
      { ...MODE_PAGE, id: 'coding_scratch', path: '/p/coding_scratch', order: 20, autoOpen: false },
    )
    seedPipelineState('pipe-1', { mode: 'coding' })
    autoOpenModePanel('pipe-1')
    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
  })

  it('标签集解析：主标签（末位=最新激活）优先命中（mode 原生数组）', async () => {
    await seedPages(MODE_PAGE, {
      type: 'pages', id: 'writing_workshop', title: '写作工坊',
      space: 'workspace', slot: 'tab', path: '/p/writing_workshop', widget: 'status_card',
      mode: 'writing', pluginId: 'mode_writing',
    })
    // 先 writing 后 coding：coding 为当前激活（末位主标签）→ 弹 coding
    seedPipelineState('pipe-1', { mode: ['writing', 'coding'] })
    autoOpenModePanel('pipe-1')
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual([
      'ws-plugin-coding_delivery',
    ])
  })

  it('标签集解析：首位标签选退 → 落到集内次位；空集回落主标签路径零动作', async () => {
    await seedPages({ ...MODE_PAGE, autoOpen: false }, {
      type: 'pages', id: 'writing_workshop', title: '写作工坊',
      space: 'workspace', slot: 'tab', path: '/p/writing_workshop', widget: 'status_card',
      mode: 'writing', pluginId: 'mode_writing',
    })
    seedPipelineState('pipe-1', { mode: ['coding', 'writing'] })
    autoOpenModePanel('pipe-1')
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual([
      'ws-plugin-writing_workshop',
    ])

    // 空集（默认兜底出生）→ 无候选零动作
    seedPipelineState('pipe-2', { mode: [] })
    autoOpenModePanel('pipe-2')
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual([
      'ws-plugin-writing_workshop',
    ])
  })

  it('string 旧形态兼容：state.mode 字符串照常命中（存量 state 归一读取）', async () => {
    await seedPages(MODE_PAGE)
    seedPipelineState('pipe-legacy', { mode: 'coding' })
    autoOpenModePanel('pipe-legacy')
    expect(useLayoutModeStore.getState().workspaceTabs.map((t) => t.id)).toEqual([
      'ws-plugin-coding_delivery',
    ])
  })
})