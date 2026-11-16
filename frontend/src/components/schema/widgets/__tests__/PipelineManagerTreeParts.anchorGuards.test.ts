/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 管道树锚点行合成的类型筛选守卫（ensureAnchorNode）：
 * 类型筛选生效时，非 task/session 的锚点条目不合成；锚点条目类型不在筛选集内
 * 也不合成（返回 undefined，调用方把条目落根平铺而不是伪造层级）。
 */

import { describe, expect, it } from 'vitest'
import { ensureAnchorNode } from '../PipelineManagerTreeParts'
import type { PipelineTreeAnchorContext } from '../PipelineManagerTreeParts'
import type { PipelineViewEntry } from '@/types/pipeline'

function makeEntry(overrides: Partial<PipelineViewEntry>): PipelineViewEntry {
  return {
    key: 'k1',
    runId: 'k1',
    kind: 'session',
    startedAt: '2026-10-28T00:00:00Z',
    ...overrides,
  } as PipelineViewEntry
}

function makeCtx(entry: PipelineViewEntry, kindFilters: Set<'task' | 'session'>): PipelineTreeAnchorContext {
  return {
    entryByKey: new Map(),
    allEntryByKey: new Map([[entry.key, entry]]),
    nodeByKey: new Map(),
    roots: [],
    kindFilters,
  }
}

describe('ensureAnchorNode — 类型筛选守卫', () => {
  it('锚点条目非 task/session（如项目行）→ 不合成锚点', () => {
    const project = makeEntry({ key: 'project-1', kind: 'project' })

    expect(ensureAnchorNode('project-1', makeCtx(project, new Set(['task'])))).toBeUndefined()
  })

  it('锚点条目类型不在筛选集内 → 不合成锚点', () => {
    const session = makeEntry({ key: 'pipe-1', kind: 'session' })

    expect(ensureAnchorNode('pipe-1', makeCtx(session, new Set(['task'])))).toBeUndefined()
  })

  it('类型在筛选集内 → 正常合成锚点节点', () => {
    const session = makeEntry({ key: 'pipe-2', kind: 'session' })
    const ctx = makeCtx(session, new Set(['session']))

    expect(ensureAnchorNode('pipe-2', ctx)).toBe('pipe-2')
    expect(ctx.nodeByKey.has('pipe-2')).toBe(true)
  })
})
