/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * CostDashboardWidget cache 维度测试（task_observability 1b）
 *
 * 可观察行为（AC）：
 * - AC-1: contextUsageStore 有 cache 数据（当前关注管道）→ 渲染缓存卡：
 *         命中率 + 命中/未命中 token + 累计 missed
 * - AC-2: cacheHistory 多轮 → 渲染会话级命中率趋势条
 * - AC-3: 无 cache 数据 → 缓存卡显示占位提示（不崩溃）
 *
 * useCostControl（HTTP 聚合数据）mock 为空——cache 维度来自 WS cost_update
 * 实时推送，不依赖 HTTP。
 */
import { render, screen } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { useContextUsageStore } from '@/stores/contextUsageStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useTerminationStore } from '@/stores/terminationStore'

// HTTP 聚合 hook mock：widget 主体（今日/本周消耗）不依赖网络
vi.mock('@/hooks/useCostControl', () => ({
  useCostControl: () => ({
    usageStats: null,
    costReport: null,
    budgetStatus: null,
    costConfig: null,
    isLoading: false,
    error: null,
    fetchUsageStatistics: vi.fn().mockResolvedValue(undefined),
    fetchCostReport: vi.fn().mockResolvedValue(undefined),
    fetchBudgetStatus: vi.fn().mockResolvedValue(undefined),
  }),
}))

import { CostDashboardWidget } from '../CostDashboardWidget'

const PID = 'pipe-cache-test'

describe('CostDashboardWidget cache 维度', () => {
  beforeEach(() => {
    useContextUsageStore.setState({ usageByPipeline: {} })
    usePipelineMessageStore.setState({ activePipelineId: PID })
    useTerminationStore.setState({ statusByPipeline: {} })
  })

  it('AC-1: 有 cache 数据 → 渲染缓存卡（命中率 + 命中/未命中 + 累计 missed）', () => {
    useContextUsageStore.getState().updateUsage(PID, {
      total_tokens: 61144,
      input_tokens: 60632,
      output_tokens: 512,
      cached_tokens: 60000,
      missed_tokens: 632,
      cache_hit_ratio: 0.9895,
      cumulative: {
        total_input: 2458025,
        total_output: 30000,
        total_cached: 2331456,
        missed: 126569,
        total_tokens: 2488025,
        cache_hit_ratio: 0.9485,
      },
    })

    render(<CostDashboardWidget />)

    const card = screen.getByTestId('cost-cache-card')
    expect(card).toBeInTheDocument()
    expect(card.textContent).toContain('99.0') // 本轮命中率（0.9895 → 99.0%）
    expect(card.textContent).toContain('60.0K') // 命中 token（formatTokens）
    expect(card.textContent).toContain('632') // 本轮未命中
    expect(card.textContent).toContain('126.6K') // 累计未命中
  })

  it('AC-2: cacheHistory 多轮 → 渲染会话级命中率趋势', () => {
    const store = useContextUsageStore.getState()
    store.updateUsage(PID, { total_tokens: 100, input_tokens: 80, output_tokens: 20, cache_hit_ratio: 0.95, cached_tokens: 76, missed_tokens: 4 })
    store.updateUsage(PID, { total_tokens: 100, input_tokens: 80, output_tokens: 20, cache_hit_ratio: 0.9, cached_tokens: 72, missed_tokens: 8 })
    store.updateUsage(PID, { total_tokens: 100, input_tokens: 80, output_tokens: 20, cache_hit_ratio: 0.5, cached_tokens: 40, missed_tokens: 40 })

    render(<CostDashboardWidget />)

    const trend = screen.getByTestId('cost-cache-trend')
    expect(trend).toBeInTheDocument()
    // 3 轮趋势点
    const bars = trend.querySelectorAll('[data-testid="cache-trend-bar"]')
    expect(bars).toHaveLength(3)
  })

  it('AC-3: 无 cache 数据 → 缓存卡占位提示，不崩溃', () => {
    render(<CostDashboardWidget />)

    const card = screen.getByTestId('cost-cache-card')
    expect(card).toBeInTheDocument()
    expect(card.textContent).toMatch(/暂无|等待/)
  })

  it('AC-4: termination_status 数据 → 渲染「剩余预算」+「收敛信号」指示器', () => {
    useTerminationStore.getState().updateStatus(PID, {
      convergence: 'converging',
      shouldStop: false,
      stopReason: '',
      remainingBudgetPercent: 73.5,
      iteration: 8,
      elapsedS: 120,
    })

    render(<CostDashboardWidget />)

    const indicator = screen.getByTestId('termination-indicator')
    expect(indicator).toBeInTheDocument()
    expect(indicator.textContent).toContain('73.5') // 剩余预算
    expect(indicator.textContent).toContain('收敛中') // convergence 信号
  })

  it('AC-5: 无 termination 数据 → 指示器占位，不崩溃', () => {
    render(<CostDashboardWidget />)

    const indicator = screen.getByTestId('termination-indicator')
    expect(indicator).toBeInTheDocument()
    expect(indicator.textContent).toMatch(/暂无|等待/)
  })

  it('AC-6: 卡死信号 → 指示器显示 stalled 状态', () => {
    useTerminationStore.getState().updateStatus(PID, {
      convergence: 'stalled',
      shouldStop: true,
      stopReason: 'stalled: tool repeat x3',
      remainingBudgetPercent: 40,
      iteration: 20,
      elapsedS: 300,
    })

    render(<CostDashboardWidget />)

    const indicator = screen.getByTestId('termination-indicator')
    expect(indicator.textContent).toContain('停滞')
  })
})
