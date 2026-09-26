/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * autoOpenModePanel —— 会话对话标签激活时的模式面板自动弹出（R92 · D-2）
 *
 * 契约：
 * - 数据源 = 共享 pipelineStates query 缓存（GET /api/v1/pipelines/state 的
 *   前端镜像，只读，不发新请求）
 * - 管道 state.mode 非空且存在 workspace/tab 等值 mode 页面声明 → 打开并激活
 *   面板页签；幂等（已开 = 激活，不重复开）
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
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { seedPipelineState } from '@/test/modePanelTestUtils'
import type { PipelineStateInfo } from '@/services/api/pipelines'

const MODE_PAGE = {
  type: 'pages' as const,
  id: 'coding_delivery',
  title: '编码交付',
  space: 'workspace' as const,
  slot: 'tab' as const,
  path: '/p/coding_delivery',
  mode: 'coding',
  pluginId: 'mode_coding',
}


describe('autoOpenModePanel', () => {
  beforeEach(() => {
    queryClient.removeQueries()
    contributionRegistry.clear()
    useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  })

  it('mode 命中声明 → 面板页签打开并激活', () => {
    contributionRegistry.register(MODE_PAGE)
    seedPipelineState('pipe-1', { mode: 'coding' })
    autoOpenModePanel('pipe-1')
    const tabs = useLayoutModeStore.getState().workspaceTabs
    expect(tabs.map((t) => t.id)).toEqual(['ws-plugin-coding_delivery'])
    expect(tabs[0]?.isActive).toBe(true)
  })

  it('幂等：已开不重复开；已开未激活 → 激活', () => {
    contributionRegistry.register(MODE_PAGE)
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

  it('无 mode 键 → 不动作', () => {
    contributionRegistry.register(MODE_PAGE)
    seedPipelineState('pipe-2', {})
    autoOpenModePanel('pipe-2')
    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
  })

  it('mode 无匹配声明 → 不动作', () => {
    contributionRegistry.register(MODE_PAGE)
    seedPipelineState('pipe-3', { mode: 'writing' })
    autoOpenModePanel('pipe-3')
    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
  })

  it('缓存无该管道 / pipelineId 为空 → 不动作', () => {
    contributionRegistry.register(MODE_PAGE)
    seedPipelineState('pipe-1', { mode: 'coding' })
    autoOpenModePanel('pipe-unknown')
    autoOpenModePanel(undefined)
    expect(useLayoutModeStore.getState().workspaceTabs).toEqual([])
  })
})
