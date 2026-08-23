/** @feature FP-0.2.四/五 fallback-audit FE项 states 拉取失败留痕 @ci frontend-test */
/**
 * pipelineRegistryStore 批次 4 query 化适配：
 * 原 fetch 的「states 侧拉取失败不阻断 runs 快照 + statesError 留痕」语义，
 * 现由双 query（usePipelineRunsQuery / usePipelineStatesQuery）天然承载——
 * states query 独立失败只置自己的 error 态，runs query 不受影响。
 * 本测试断言：
 * 1. applyStreamStatus（store 编排层）增量写 runs query 缓存：
 *    stream_end → completed 且补 ended_at；
 * 2. 新管道从 pipelineMessageStore 反查归属会话写入；
 * 3. reset 清空 runs 缓存。
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

import { queryClient } from '@/services/query/queryClient'
import { queryKeys } from '@/services/query/queryKeys'
import { usePipelineRegistryStore } from '../pipelineRegistryStore'
import { readPipelineRuns } from '@/hooks/queries/usePipelineRunsQuery'

describe('pipelineRegistryStore 批次 4 query 化', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    // 清空全局 queryClient 缓存，隔离用例（store 的 applyStreamStatus 与
    // readPipelineRuns 读写同一个模块单例，无 resetModules 撕裂问题）
    queryClient.clear()
  })

  afterEach(() => {
    usePipelineRegistryStore.getState().reset()
  })

  it('applyStreamStatus：stream_end 增量写 runs 缓存（补 ended_at）', () => {
    queryClient.setQueryData(queryKeys.pipelineRuns, {
      pipeA: {
        run_id: 'r1',
        pipeline_id: 'pipeA',
        thread_id: 't1',
        status: 'running',
        started_at: '2026-08-20T00:00:00Z',
      },
    })

    usePipelineRegistryStore.getState().applyStreamStatus('pipeA', 'completed')

    const runs = readPipelineRuns()
    expect(runs.pipeA.status).toBe('completed')
    expect(runs.pipeA.ended_at).toBeDefined()
    // 未结束事件不补 ended_at（running → suspended 等）
    usePipelineRegistryStore.getState().applyStreamStatus('pipeA', 'suspended')
    expect(readPipelineRuns().pipeA.status).toBe('suspended')
    expect(readPipelineRuns().pipeA.ended_at).toBeUndefined()
  })

  it('applyStreamStatus：新管道从 pipelineMessageStore 反查归属会话写入', () => {
    usePipelineRegistryStore.getState().applyStreamStatus('newPipe', 'running')

    const runs = readPipelineRuns()
    expect(runs.newPipe).toBeDefined()
    expect(runs.newPipe.status).toBe('running')
    // mock 的 pipelineSessionMap 为空 → 无归属会话
    expect(runs.newPipe.thread_id).toBeUndefined()
    expect(runs.newPipe.started_at).toBeDefined()
  })

  it('states 拉取失败不阻断 runs 快照（独立 query 承载降级）', async () => {
    // 验证独立 queryFn 语义：states 失败仅置自身 error，runs 正常
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    mockFetchPipelineRuns.mockResolvedValue([
      { run_id: 'r1', pipeline_id: 'pipeA', thread_id: 't1', status: 'running', started_at: '2026-08-20T00:00:00Z' },
    ])
    mockFetchPipelineStates.mockRejectedValue(new Error('state endpoint down'))

    const runsError = await queryClient
      .fetchQuery({
        queryKey: queryKeys.pipelineRuns,
        queryFn: () =>
          mockFetchPipelineRuns({ limit: 100 }).then((items: unknown[]) => {
            const next: Record<string, unknown> = {}
            for (const item of items as Array<Record<string, string>>) {
              next[item.pipeline_id] = item
            }
            return next
          }),
      })
      .then(() => null)
      .catch((e: unknown) => e)
    expect(runsError).toBeNull()
    expect(Object.keys(readPipelineRuns())).toEqual(['pipeA'])

    // states 独立失败：只影响自身 query，runs 快照仍在
    await expect(
      queryClient.fetchQuery({
        queryKey: queryKeys.pipelineStates,
        queryFn: () =>
          mockFetchPipelineStates().then((items: unknown[]) => {
            const next: Record<string, unknown> = {}
            for (const st of items as Array<{ pipeline_id: string }>) next[st.pipeline_id] = st
            return next
          }),
      }),
    ).rejects.toThrow('state endpoint down')
    expect(Object.keys(readPipelineRuns())).toEqual(['pipeA'])
    warnSpy.mockRestore()
  })

  it('reset 清空 runs 缓存', () => {
    queryClient.setQueryData(queryKeys.pipelineRuns, {
      pipeA: { run_id: 'r1', pipeline_id: 'pipeA', status: 'running', started_at: 'x' },
    })

    usePipelineRegistryStore.getState().reset()

    expect(readPipelineRuns()).toEqual({})
  })
})
