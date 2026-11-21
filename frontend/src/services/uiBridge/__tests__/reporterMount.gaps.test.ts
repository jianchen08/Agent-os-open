/** @feature FP-0.2.侧边栏UI对象化 侧边栏UI对象化P1生产接线 @ci: frontend-test
 * reporterMount 补充缺口测试（实现方补充件，不动签发件）：快照 items 的插件页
 * 拼装链（activity-bar 贡献页进快照）——改动行 diff coverage 100% 前提。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { queryClient } from '@/services/query/queryClient'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { mountUiBridgeReporter } from '../reporterMount'
import type { UiBridgeReporterHandle } from '../reporterMount'

const wsSubscribeMock = vi.fn()
const wsUnsubscribeMock = vi.fn()
const apiPostMock = vi.fn()

vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: {
    subscribe: (event: string, handler: (data: unknown) => void) => {
      wsSubscribeMock(event, handler)
    },
    unsubscribe: (event: string, handler: (data: unknown) => void) => {
      wsUnsubscribeMock(event, handler)
    },
    status: 'connected',
  },
}))

vi.mock('@/services/api/client', () => ({
  default: { post: (...args: unknown[]) => apiPostMock(...args) },
  apiClient: { post: (...args: unknown[]) => apiPostMock(...args) },
}))

const handles: UiBridgeReporterHandle[] = []

beforeEach(() => {
  vi.useFakeTimers()
  wsSubscribeMock.mockClear()
  wsUnsubscribeMock.mockClear()
  apiPostMock.mockClear()
  apiPostMock.mockResolvedValue({})
  localStorage.clear()
  contributionRegistry.clear()
})

afterEach(() => {
  for (const handle of handles.splice(0)) handle.dispose()
  contributionRegistry.clear()
  queryClient.clear()
  localStorage.clear()
  vi.useRealTimers()
})

describe('快照 items 插件页拼装（activity-bar 贡献页）', () => {
  it('activity-bar 插件贡献页进快照 items（kind=plugin_page），非 activity-bar 槽位不进', async () => {
    contributionRegistry.register({
      type: 'pages',
      id: 'gap.plugin.page',
      space: 'workspace',
      slot: 'activity-bar',
      title: '缺口插件页',
      widget: 'custom_widget',
      order: 3,
    })
    contributionRegistry.register({
      type: 'pages',
      id: 'gap.tab.page',
      space: 'workspace',
      slot: 'tab',
      title: '工作区页签页',
      widget: 'another_widget',
      order: 4,
    })
    const send = vi.fn().mockResolvedValue(undefined)
    handles.push(mountUiBridgeReporter({ send }))

    await vi.advanceTimersByTimeAsync(0)
    expect(send).toHaveBeenCalledTimes(1)
    const report = send.mock.calls[0][0] as {
      items: Array<{ id: string; kind: string }>
    }
    const ids = report.items.map((item) => item.id)
    expect(ids).toContain('gap.plugin.page')
    expect(ids).not.toContain('gap.tab.page')
    const pluginItem = report.items.find((item) => item.id === 'gap.plugin.page')
    expect(pluginItem?.kind).toBe('plugin_page')
  })
})
