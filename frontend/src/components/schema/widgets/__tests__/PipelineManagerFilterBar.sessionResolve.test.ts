/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 管道条目归属会话解析（resolveEntrySessionId）补测：
 * threadId 命中会话列表为第一真值；threadId 未命中（自环子任务管道）时回落
 * 血缘 originSessionId；都未命中即无归属（null）。
 */

import { describe, expect, it } from 'vitest'
import { entryIdHit, entryTextFieldHit, matchesEntryFilters, resolveEntrySessionId } from '../PipelineManagerFilterBar'
import type { Session } from '@/types/models'
import type { PipelineViewEntry } from '@/types/pipeline'

function entry(overrides: Partial<PipelineViewEntry> = {}): PipelineViewEntry {
  return {
    key: 'pipe-1',
    runId: 'run-1',
    kind: 'session',
    startedAt: '2026-10-28T00:00:00Z',
    ...overrides,
  } as PipelineViewEntry
}

const sessions = [{ id: 'sess-root', title: '根会话' }] as Session[]

describe('resolveEntrySessionId — 会话归属解析', () => {
  it('threadId 命中会话列表 → 直接取 threadId', () => {
    expect(resolveEntrySessionId(entry({ threadId: 'sess-root' }), sessions)).toBe('sess-root')
  })

  it('threadId 未命中但血缘 originSessionId 命中 → 回落血缘会话', () => {
    expect(
      resolveEntrySessionId(
        entry({ threadId: 'pipe-self-loop', originSessionId: 'sess-root' }),
        sessions,
      ),
    ).toBe('sess-root')
  })

  it('无任何命中（孤儿管道）→ null（不猜归属）', () => {
    expect(
      resolveEntrySessionId(entry({ threadId: 'pipe-x', originSessionId: 'sess-missing' }), sessions),
    ).toBeNull()
  })
})


describe('matchesEntryFilters — 管道 ID 搜索（用户实测 2026-11-19）', () => {
  const base = {
    kindFilters: new Set<'task' | 'session'>(),
    statusFilters: new Set<string>(['running']),
    sessionFilter: null,
    keyword: '',
  }

  it('终态管道在缺省「只看运行中」档下，输入完整管道 ID 仍命中（ID = 定位动作，不吃浏览筛选）', () => {
    const done = entry({ key: 'abc123def456', pipelineId: 'abc123def456', status: 'completed', name: '终态管道' })
    expect(matchesEntryFilters(done, { ...base, keyword: 'abc123def456' })).toBe(true)
  })

  it('部分匹配生效：`abc123` 命中 `abc123def456`，且大小写不敏感', () => {
    const done = entry({ key: 'abc123def456', pipelineId: 'abc123def456', status: 'failed', name: '失败管道' })
    expect(matchesEntryFilters(done, { ...base, keyword: 'abc123' })).toBe(true)
    expect(matchesEntryFilters(done, { ...base, keyword: 'ABC123' })).toBe(true)
    expect(matchesEntryFilters(done, { ...base, keyword: 'abc124' })).toBe(false)
  })

  it('运行 ID / 任务 ID 同源可搜（详情面板复制哪个都能命中）', () => {
    const row = entry({ key: 'p1', pipelineId: '9f9f9f9f9f9f', taskId: '7e7e7e7e7e7e', runId: 'run-uuid-77', status: 'completed', name: '三 ID 行' })
    expect(entryIdHit(row, '9f9f')).toBe(true)
    expect(entryIdHit(row, '7e7e')).toBe(true)
    expect(entryIdHit(row, 'run-uuid')).toBe(true)
  })

  it('名称命中仍与状态筛选 AND（名称搜索口径不变）', () => {
    const done = entry({ key: 'p2', pipelineId: 'p2', status: 'completed', name: '验证C3-裁撤冒烟等价版' })
    expect(entryTextFieldHit(done, '验证c3')).toBe(true)
    expect(matchesEntryFilters(done, { ...base, keyword: '验证C3' })).toBe(false)
    expect(matchesEntryFilters(done, { ...base, keyword: '验证C3', statusFilters: new Set<string>() })).toBe(true)
  })

  it('下辖运行中的祖先在「运行中」档下保留（链活跃聚合），无下辖则滤掉', () => {
    const parent = entry({ key: 'parent-1', pipelineId: 'parent-1', status: 'suspended', name: '父层A' })
    const other = entry({ key: 'parent-2', pipelineId: 'parent-2', status: 'suspended', name: '无关父层' })
    const chain = new Set<string>(['parent-1'])
    expect(matchesEntryFilters(parent, { ...base, runningChainKeys: chain })).toBe(true)
    expect(matchesEntryFilters(other, { ...base, runningChainKeys: chain })).toBe(false)
  })
})
