/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 管道树锚点行合成（ensureAnchorNode）契约。
 *
 * 用户裁定 2026-11-20：任务/会话类型筛选整体退役，锚点合成不再有类型守卫——
 * 任何 kind 的条目只要在全量条目里存在、且视图内未含，就按锚点合成挂根
 * （避免「窄筛视图把主管道滤掉后，子任务平级挂出、归属不可见」）。
 *
 * 锁三点：视图外条目与其类型无关地合成、已建成的节点直接复用（不重复挂根）、
 * 全量条目里根本没有的 key 返回 undefined（不伪造节点）。
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

function makeCtx(entry: PipelineViewEntry): PipelineTreeAnchorContext {
  return {
    entryByKey: new Map(),
    allEntryByKey: new Map([[entry.key, entry]]),
    nodeByKey: new Map(),
    roots: [],
  }
}

describe('ensureAnchorNode — 锚点合成（无类型守卫）', () => {
  it('项目行也能合成锚点（类型维退役后不再排除非 task/session）', () => {
    const project = makeEntry({ key: 'project-1', kind: 'project' })
    const ctx = makeCtx(project)

    expect(ensureAnchorNode('project-1', ctx)).toBe('project-1')
    expect(ctx.nodeByKey.has('project-1')).toBe(true)
    expect(ctx.roots).toHaveLength(1)
  })

  it('会话行合成锚点（两组不同 kind 都成立，证明不看类型）', () => {
    const session = makeEntry({ key: 'pipe-1', kind: 'session' })
    const ctx = makeCtx(session)

    expect(ensureAnchorNode('pipe-1', ctx)).toBe('pipe-1')
    expect(ctx.nodeByKey.has('pipe-1')).toBe(true)
  })

  it('已建节点直接复用，不重复挂根（幂等）', () => {
    const entry = makeEntry({ key: 'pipe-2', kind: 'session' })
    const ctx = makeCtx(entry)

    ensureAnchorNode('pipe-2', ctx)
    const again = ensureAnchorNode('pipe-2', ctx)

    expect(again).toBe('pipe-2')
    expect(ctx.roots).toHaveLength(1)
  })

  it('全量条目里没有的 key → undefined（不伪造节点）', () => {
    const entry = makeEntry({ key: 'pipe-3', kind: 'session' })
    const ctx = makeCtx(entry)

    expect(ensureAnchorNode('missing-key', ctx)).toBeUndefined()
    expect(ctx.roots).toHaveLength(0)
  })
})
