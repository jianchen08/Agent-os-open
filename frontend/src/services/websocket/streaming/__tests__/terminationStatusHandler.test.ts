/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * handleTerminationStatus 测试（task_observability 1c）
 *
 * termination_advisor 插件每轮经 frontend.emit 推送 termination_status：
 * - 按 pipeline 分桶写入 terminationStore（「剩余预算」+「收敛信号」数据源）
 * - remaining_budget_percent 为 null（预算信号缺失）→ 保持 null（前端显示「未启用」）
 * - 缺 pipeline_id → 跳过
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { handleTerminationStatus } from '../lifecycleHandlers'
import { useTerminationStore } from '@/stores/terminationStore'

describe('handleTerminationStatus', () => {
  beforeEach(() => {
    useTerminationStore.setState({ statusByPipeline: {} })
  })

  it('正常 payload：按 pipeline 写入收敛状态', () => {
    handleTerminationStatus({
      type: 'termination_status',
      data: {
        pipeline_id: 'pipe-term',
        thread_id: 'thread-1',
        convergence: 'converging',
        should_stop: false,
        stop_reason: '',
        remaining_budget_percent: 87.5,
        iteration: 3,
        elapsed_s: 45,
      },
    })
    const status = useTerminationStore.getState().getStatus('pipe-term')
    expect(status).toBeDefined()
    expect(status?.convergence).toBe('converging')
    expect(status?.shouldStop).toBe(false)
    expect(status?.remainingBudgetPercent).toBe(87.5)
    expect(status?.iteration).toBe(3)
  })

  it('remaining_budget_percent 缺失（预算信号未启用）→ null', () => {
    handleTerminationStatus({
      type: 'termination_status',
      data: {
        pipeline_id: 'pipe-nb',
        convergence: 'converging',
        should_stop: false,
        remaining_budget_percent: null,
        iteration: 1,
        elapsed_s: 2,
      },
    })
    expect(useTerminationStore.getState().getStatus('pipe-nb')?.remainingBudgetPercent).toBeNull()
  })

  it('缺 pipeline_id 时跳过', () => {
    handleTerminationStatus({
      type: 'termination_status',
      data: { convergence: 'stalled', should_stop: true },
    })
    expect(Object.keys(useTerminationStore.getState().statusByPipeline)).toHaveLength(0)
  })

  it('多 pipeline 独立分桶', () => {
    handleTerminationStatus({
      type: 'termination_status',
      data: { pipeline_id: 'pipe-a', convergence: 'converging', should_stop: false },
    })
    handleTerminationStatus({
      type: 'termination_status',
      data: { pipeline_id: 'pipe-b', convergence: 'budget_critical', should_stop: false },
    })
    expect(useTerminationStore.getState().getStatus('pipe-a')?.convergence).toBe('converging')
    expect(useTerminationStore.getState().getStatus('pipe-b')?.convergence).toBe('budget_critical')
  })
})
