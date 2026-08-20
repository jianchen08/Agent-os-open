/** @feature FP-兜底反模式修复.FE6 states 拉取失败留痕 @ci frontend-test */
/**
 * pipelineRegistryStore.fetch：states 侧拉取失败不再静默吞掉——
 * statesError 置位（供 UI 提示"状态可能不全"）+ console.warn，
 * runs 快照不受影响（降级不阻断）。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mockFetchPipelineRuns = vi.fn()
const mockFetchPipelineStates = vi.fn()

vi.mock('@/services/api/pipelines', () => ({
  fetchPipelineRuns: (...args: unknown[]) => mockFetchPipelineRuns(...args),
  fetchPipelineStates: (...args: unknown[]) => mockFetchPipelineStates(...args),
}))

vi.mock('@/stores/pipelineMessageStore', () => ({
  usePipelineMessageStore: {
    getState: () => ({ pipelineSessionMap: {}, pipelines: {} }),
  },
}))

import { usePipelineRegistryStore } from '../pipelineRegistryStore'

describe('pipelineRegistryStore.fetch 的 states 降级留痕（FE6）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    usePipelineRegistryStore.getState().reset()
  })

  afterEach(() => {
    usePipelineRegistryStore.getState().reset()
  })

  it('states 拉取失败：runs 正常合并、states 置空、statesError 置位并 warn', async () => {
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    mockFetchPipelineRuns.mockResolvedValue([
      { run_id: 'r1', pipeline_id: 'pipeA', thread_id: 't1', status: 'running', started_at: '2026-08-20T00:00:00Z' },
    ])
    mockFetchPipelineStates.mockRejectedValue(new Error('state endpoint down'))

    await usePipelineRegistryStore.getState().fetch()

    const s = usePipelineRegistryStore.getState()
    // runs 快照不受 state 失败影响
    expect(Object.keys(s.runs)).toEqual(['pipeA'])
    expect(s.states).toEqual({})
    // 降级可见：错误位 + warn 留痕
    expect(s.statesError).toBe('state endpoint down')
    expect(s.error).toBeNull()
    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('拉取管道 state 失败'),
      expect.any(Error),
    )
    warnSpy.mockRestore()
  })

  it('states 拉取成功：statesError 清空', async () => {
    usePipelineRegistryStore.setState({ statesError: '旧错误' })
    mockFetchPipelineRuns.mockResolvedValue([])
    mockFetchPipelineStates.mockResolvedValue([
      { pipeline_id: 'pipeB', source: 'memory', state: { current_phase: 'main' } },
    ])

    await usePipelineRegistryStore.getState().fetch()

    const s = usePipelineRegistryStore.getState()
    expect(s.statesError).toBeNull()
    expect(Object.keys(s.states)).toEqual(['pipeB'])
  })
})
