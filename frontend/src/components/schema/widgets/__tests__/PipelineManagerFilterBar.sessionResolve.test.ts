/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 管道条目归属会话解析（resolveEntrySessionId）补测：
 * threadId 命中会话列表为第一真值；threadId 未命中（自环子任务管道）时回落
 * 血缘 originSessionId；都未命中即无归属（null）。
 */

import { describe, expect, it } from 'vitest'
import { resolveEntrySessionId } from '../PipelineManagerFilterBar'
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
