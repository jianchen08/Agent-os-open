/**
 * handleCostUpdate 测试 - cost_update 事件写入 contextUsageStore
 *
 * 验证：
 * 1. 正常 payload → 按 pipeline 分桶写入单轮 token
 * 2. total_tokens 为 0（tool_execute 残留）→ 不覆盖已有值
 * 3. 缺 pipeline_id → 跳过
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { handleCostUpdate } from '../lifecycleHandlers'
import { useContextUsageStore } from '@/stores/contextUsageStore'

describe('handleCostUpdate', () => {
  beforeEach(() => {
    // 重置 store，隔离每个用例
    const { usageByPipeline } = useContextUsageStore.getState()
    for (const pid of Object.keys(usageByPipeline)) {
      useContextUsageStore.getState().clearUsage(pid)
    }
  })

  it('正常 payload：按 pipeline 分桶写入单轮 token', () => {
    handleCostUpdate({
      type: 'cost_update',
      data: {
        pipeline_id: 'pipe-001',
        total_tokens: 1500,
        input_tokens: 1200,
        output_tokens: 300,
      },
    })
    const usage = useContextUsageStore.getState().getUsage('pipe-001')
    expect(usage).toBeDefined()
    expect(usage?.totalTokens).toBe(1500)
    expect(usage?.promptTokens).toBe(1200)
    expect(usage?.completionTokens).toBe(300)
  })

  it('total_tokens 为 0 时不覆盖（tool_execute 轮残留兜底）', () => {
    // 先写入有效值
    handleCostUpdate({
      type: 'cost_update',
      data: { pipeline_id: 'pipe-002', total_tokens: 800, input_tokens: 600, output_tokens: 200 },
    })
    // 收到 0 值（后端 tool_execute 轮已跳过，这里兜底防覆盖）
    handleCostUpdate({
      type: 'cost_update',
      data: { pipeline_id: 'pipe-002', total_tokens: 0, input_tokens: 0, output_tokens: 0 },
    })
    const usage = useContextUsageStore.getState().getUsage('pipe-002')
    expect(usage?.totalTokens).toBe(800)
  })

  it('缺 pipeline_id 时跳过，不写入', () => {
    handleCostUpdate({
      type: 'cost_update',
      data: { total_tokens: 1500, input_tokens: 1200, output_tokens: 300 },
    })
    // 不应有任何 pipeline 被写入
    const { usageByPipeline } = useContextUsageStore.getState()
    expect(Object.keys(usageByPipeline)).toHaveLength(0)
  })

  it('多 pipeline 独立分桶', () => {
    handleCostUpdate({
      type: 'cost_update',
      data: { pipeline_id: 'pipe-A', total_tokens: 100, input_tokens: 80, output_tokens: 20 },
    })
    handleCostUpdate({
      type: 'cost_update',
      data: { pipeline_id: 'pipe-B', total_tokens: 200, input_tokens: 150, output_tokens: 50 },
    })
    expect(useContextUsageStore.getState().getUsage('pipe-A')?.totalTokens).toBe(100)
    expect(useContextUsageStore.getState().getUsage('pipe-B')?.totalTokens).toBe(200)
  })
})
