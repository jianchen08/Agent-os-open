// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * usePipelineRunsQuery 纯逻辑测试
 *
 * 覆盖：entryKey（pipeline_id 优先回退 run_id）、mapRunsToRecord（同管道多条
 * run 取 started_at 最新）、mapStatesToRecord 索引、缓存读写与失效。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

const { mockGetQueryData, mockSetQueryData, mockInvalidate, mockFetchQuery } = vi.hoisted(() => ({
  mockGetQueryData: vi.fn(),
  mockSetQueryData: vi.fn(),
  mockInvalidate: vi.fn(),
  mockFetchQuery: vi.fn(),
}))

vi.mock('@/services/query/queryClient', () => ({
  queryClient: {
    getQueryData: mockGetQueryData,
    setQueryData: mockSetQueryData,
    invalidateQueries: mockInvalidate,
    fetchQuery: mockFetchQuery,
  },
}))

vi.mock('@/services/query/queryKeys', () => ({
  queryKeys: {
    pipelineRuns: ['pipeline-runs'],
    pipelineStates: ['pipeline-states'],
  },
}))

import {
  mapRunsToRecord,
  readPipelineRuns,
  updatePipelineRunsCache,
  invalidatePipelineRuns,
  invalidatePipelineStates,
} from '@/hooks/queries/usePipelineRunsQuery'

const run = (pipelineId: string, runId: string, startedAt: string) => ({
  pipeline_id: pipelineId,
  run_id: runId,
  started_at: startedAt,
})

describe('mapRunsToRecord - runs 归并', () => {
  it('同管道多条 run → 取 started_at 最新的一条', () => {
    const record = mapRunsToRecord([
      run('p1', 'r1', '2026-08-01T00:00:00Z'),
      run('p1', 'r2', '2026-08-02T00:00:00Z'),
      run('p2', 'r3', '2026-08-03T00:00:00Z'),
    ])
    expect(Object.keys(record).length).toBe(2)
    expect(record.p1.run_id).toBe('r2') // 最新
    expect(record.p2.run_id).toBe('r3')
  })

  it('pipeline_id 缺失 → 回退 run_id 作为 key', () => {
    const record = mapRunsToRecord([
      { run_id: 'orphan-run', started_at: '2026-08-01T00:00:00Z' } as any,
    ])
    expect(record['orphan-run']).toBeDefined()
  })

  it('空数组 → 空 Record', () => {
    expect(mapRunsToRecord([])).toEqual({})
  })

  it('同 started_at 时后出现的覆盖先出现的（>= 语义）', () => {
    const record = mapRunsToRecord([
      run('p1', 'r1', '2026-08-01T00:00:00Z'),
      run('p1', 'r2', '2026-08-01T00:00:00Z'),
    ])
    expect(record.p1.run_id).toBe('r2')
  })
})

describe('缓存读写', () => {
  beforeEach(() => {
    mockGetQueryData.mockReset()
    mockSetQueryData.mockReset()
    mockInvalidate.mockReset()
    mockFetchQuery.mockReset()
  })

  it('readPipelineRuns 无缓存 → 空对象', () => {
    mockGetQueryData.mockReturnValue(undefined)
    expect(readPipelineRuns()).toEqual({})
    expect(mockGetQueryData).toHaveBeenCalledWith(['pipeline-runs'])
  })

  it('readPipelineRuns 有缓存 → 返回缓存', () => {
    mockGetQueryData.mockReturnValue({ p1: run('p1', 'r1', 'x') })
    expect(readPipelineRuns().p1.run_id).toBe('r1')
  })

  it('updatePipelineRunsCache 无缓存视同 {}，updater 结果落盘', () => {
    mockSetQueryData.mockImplementation((_k, fn: any) => fn({}))
    updatePipelineRunsCache((prev) => ({ ...prev, p9: run('p9', 'r9', 'x') }))
    expect(mockSetQueryData).toHaveBeenCalledWith(
      ['pipeline-runs'],
      expect.any(Function),
    )
  })

  it('invalidatePipelineRuns / invalidatePipelineStates 按各自 key 失效', () => {
    invalidatePipelineRuns()
    expect(mockInvalidate).toHaveBeenCalledWith({ queryKey: ['pipeline-runs'] })
    invalidatePipelineStates()
    expect(mockInvalidate).toHaveBeenCalledWith({ queryKey: ['pipeline-states'] })
  })
})
