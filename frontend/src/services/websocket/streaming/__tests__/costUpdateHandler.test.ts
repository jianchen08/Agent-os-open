/**
 * handleCostUpdate 测试 - cost_update 事件写入 contextUsageStore
 *
 * 验证：
 * 1. 正常 payload → 按 pipeline 分桶写入单轮 token
 * 2. total_tokens 为 0（tool_execute 残留）→ 不覆盖已有值
 * 3. 缺 pipeline_id → 跳过
 * 4. cache 维度（task_observability 1b）：cached/missed/ratio/cumulative 写入
 *    + 会话级命中率趋势（cacheHistory）
 * 5. 命中率骤降（如 95% → 50%）→ 通知提示一次，恢复后可再次提示
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { handleCostUpdate } from '../lifecycleHandlers'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { useNotificationStore } from '@/stores/notificationStore'

/** 构造带 cache 维度的 cost_update 事件 */
function cacheEvent(pipelineId: string, ratio: number, cached: number, input: number) {
  return {
    type: 'cost_update',
    data: {
      pipeline_id: pipelineId,
      total_tokens: input + 10,
      input_tokens: input,
      output_tokens: 10,
      cached_tokens: cached,
      missed_tokens: input - cached,
      cache_hit_ratio: ratio,
      cumulative: {
        total_input: input * 2,
        total_output: 20,
        total_cached: cached * 2,
        missed: (input - cached) * 2,
        total_tokens: input * 2 + 20,
        cache_hit_ratio: ratio,
      },
    },
  }
}

describe('handleCostUpdate', () => {
  beforeEach(() => {
    // 重置 store，隔离每个用例
    const { usageByPipeline } = useContextUsageStore.getState()
    for (const pid of Object.keys(usageByPipeline)) {
      useContextUsageStore.getState().clearUsage(pid)
    }
    useNotificationStore.getState().clearAll()
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

  describe('cache 维度（task_observability 1b）', () => {
    it('cached/missed/ratio/cumulative 写入 usage 条目', () => {
      handleCostUpdate(cacheEvent('pipe-cache', 0.8, 8000, 10000))
      const usage = useContextUsageStore.getState().getUsage('pipe-cache')
      expect(usage?.cachedTokens).toBe(8000)
      expect(usage?.missedTokens).toBe(2000)
      expect(usage?.hitRatio).toBe(0.8)
      expect(usage?.cumulative).toMatchObject({
        total_input: 20000,
        total_cached: 16000,
        missed: 4000,
        cache_hit_ratio: 0.8,
      })
    })

    it('会话级命中率趋势：每轮追加 cacheHistory', () => {
      handleCostUpdate(cacheEvent('pipe-hist', 0.95, 9500, 10000))
      handleCostUpdate(cacheEvent('pipe-hist', 0.9, 9000, 10000))
      const usage = useContextUsageStore.getState().getUsage('pipe-hist')
      expect(usage?.cacheHistory).toHaveLength(2)
      expect(usage?.cacheHistory?.[0]).toMatchObject({ hitRatio: 0.95, missedTokens: 500 })
      expect(usage?.cacheHistory?.[1]).toMatchObject({ hitRatio: 0.9, missedTokens: 1000 })
    })

    it('无 cache 字段的旧事件不产生趋势噪音（history 不追加）', () => {
      handleCostUpdate({
        type: 'cost_update',
        data: { pipeline_id: 'pipe-legacy', total_tokens: 100, input_tokens: 80, output_tokens: 20 },
      })
      const usage = useContextUsageStore.getState().getUsage('pipe-legacy')
      expect(usage?.cacheHistory ?? []).toHaveLength(0)
    })

    it('命中率骤降（95% → 50%）通知一次，持续低位不重复', () => {
      handleCostUpdate(cacheEvent('pipe-drop', 0.95, 9500, 10000))
      expect(useNotificationStore.getState().notifications).toHaveLength(0)
      // 骤降：0.95 → 0.50（降 45pp 且当前 <70%）
      handleCostUpdate(cacheEvent('pipe-drop', 0.5, 5000, 10000))
      const notes = useNotificationStore.getState().notifications
      expect(notes).toHaveLength(1)
      expect(notes[0].title).toContain('缓存')
      // 持续低位（0.48）：不重复提示
      handleCostUpdate(cacheEvent('pipe-drop', 0.48, 4800, 10000))
      expect(useNotificationStore.getState().notifications).toHaveLength(1)
    })

    it('恢复后再次骤降可再次提示', () => {
      handleCostUpdate(cacheEvent('pipe-re', 0.95, 9500, 10000))
      handleCostUpdate(cacheEvent('pipe-re', 0.5, 5000, 10000))
      expect(useNotificationStore.getState().notifications).toHaveLength(1)
      // 恢复到高位 → 解除「已提示」状态
      handleCostUpdate(cacheEvent('pipe-re', 0.95, 9500, 10000))
      expect(useNotificationStore.getState().notifications).toHaveLength(1)
      // 再次骤降 → 第二次提示
      handleCostUpdate(cacheEvent('pipe-re', 0.4, 4000, 10000))
      expect(useNotificationStore.getState().notifications).toHaveLength(2)
    })

    it('缓降（95% → 75%）不提示（降幅 <30pp 阈值）', () => {
      handleCostUpdate(cacheEvent('pipe-slow', 0.95, 9500, 10000))
      handleCostUpdate(cacheEvent('pipe-slow', 0.75, 7500, 10000))
      expect(useNotificationStore.getState().notifications).toHaveLength(0)
    })
  })
})
