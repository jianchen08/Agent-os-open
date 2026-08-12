/**
 * GrowthLoop → commandDispatcher transport 注入测试（阶段0）
 *
 * 背景：commandDispatcher.setTransport 注释声称「启动期由 GrowthLoop 注入」，
 * 但生产代码从未调用 → executeCommand 的 `if (this.transport)` 分支恒不进入，
 * 命令面板点击/快捷键/右键菜单触发后只弹 modal、不调内核能力（静默 no-op）。
 *
 * 本测试聚焦「GrowthLoop 注入」这一件事（CommandDispatcher 本体行为已在
 * commandDispatcher.test.ts 覆盖）：initializeGrowthLoop 之后，executeCommand
 * 必须把命令经 transport 路由到内核（apiClient.post /api/v1/actions/execute）。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
// Mock 内核 transport 出口：apiClient（默认导出）
vi.mock('@/services/api/client', () => ({
  default: { post: vi.fn() },
}))
// Mock schema 拉取（GrowthLoop.reloadContributionRegistry 的数据源）
vi.mock('@/services/api/schema', () => ({
  getSchema: vi.fn(),
}))
import apiClient from '@/services/api/client'
import { getSchema } from '@/services/api/schema'
import { initializeGrowthLoop } from '@/services/modules/GrowthLoop'
import { commandDispatcher } from '@/services/schema/commandDispatcher'

describe('GrowthLoop — commandDispatcher transport 注入', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.mocked(getSchema).mockResolvedValue({
      agents: [],
      pipelines: [],
      tools: [],
      routes: {},
      plugin_configs: [],
      plugin_contributes: [],
    })
  })

  it('initializeGrowthLoop 后 executeCommand 经 transport 调用内核 /api/v1/actions/execute', async () => {
    await initializeGrowthLoop()

    await commandDispatcher.executeCommand('cost.showReport', { metric: 'tokens' })

    expect(apiClient.post).toHaveBeenCalledWith('/api/v1/actions/execute', {
      action: 'cost.showReport',
      args: { metric: 'tokens' },
    })
  })

  it('initializeGrowthLoop 后命令执行不再静默 no-op（transport 恒被调用）', async () => {
    await initializeGrowthLoop()

    await commandDispatcher.executeCommand('plugin.run')

    expect(apiClient.post).toHaveBeenCalledTimes(1)
    expect(apiClient.post).toHaveBeenCalledWith('/api/v1/actions/execute', {
      action: 'plugin.run',
      args: undefined,
    })
  })
})
