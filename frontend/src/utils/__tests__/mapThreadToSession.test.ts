/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * mapThreadToSession 映射测试（扫描批K S2：兜底伪造收口）
 *
 * 行为契约：
 * - 时间戳为后端必返字段，缺失抛协议违反错误（不再用 new Date() 伪造）；
 * - status 不再伪造 'active'（两源皆缺时 undefined，由消费方按未知处理）；
 * - title 缺省回退「未命名会话」仅为展示占位文案。
 */
import { describe, expect, it } from 'vitest'
import { mapThreadToSession, type ThreadStateResponse } from '@/utils/mappers'
import type { Session } from '@/types/models'

const baseThread = {
  thread_id: 'th-1',
  current_state: 'idle',
  intent: null,
  created_at: '2026-08-01T00:00:00Z',
  updated_at: '2026-08-02T00:00:00Z',
}

describe('mapThreadToSession 基础映射', () => {
  it('字段直映：id/标题链/时间戳/current_state', () => {
    const s = mapThreadToSession({ ...baseThread, title: '我的会话', intent: '做X' })
    expect(s.id).toBe('th-1')
    expect(s.title).toBe('我的会话')
    expect(s.createdAt).toBe('2026-08-01T00:00:00Z')
    expect(s.updatedAt).toBe('2026-08-02T00:00:00Z')
    expect(s.status).toBe('idle')
    expect(s.messageCount).toBeGreaterThanOrEqual(0)
  })

  it('title 缺失回退 intent，再缺失回退展示占位文案', () => {
    expect(mapThreadToSession({ ...baseThread, title: null, intent: '意图A' }).title).toBe('意图A')
    expect(mapThreadToSession({ ...baseThread, title: null, intent: null }).title).toBe('未命名会话')
  })

  it('status 链：legacy status 优先；仅 current_state 时用它', () => {
    expect(
      mapThreadToSession({ ...baseThread, status: 'archived' } as typeof baseThread & { status?: string }).status,
    ).toBe('archived')
    // Thread 型无 status 字段 → 走 current_state
    expect(mapThreadToSession({ ...baseThread }).status).toBe('idle')
  })
})

describe('mapThreadToSession 协议违反收口（S2 改判行为）', () => {
  it('status 与 current_state 双缺 → undefined，不伪造 active', () => {
    const s = mapThreadToSession({ ...baseThread, current_state: '', metadata: {} } as Partial<ThreadStateResponse> as ThreadStateResponse)
    expect(s.status).toBeUndefined()
  })

  it('created_at / updated_at 缺失 → 抛协议违反错误', () => {
    expect(() =>
      mapThreadToSession({ ...baseThread, created_at: '' } as Partial<ThreadStateResponse> as ThreadStateResponse),
    ).toThrow(/created_at/)
    expect(() =>
      mapThreadToSession({ ...baseThread, updated_at: '' } as Partial<ThreadStateResponse> as ThreadStateResponse),
    ).toThrow(/updated_at/)
  })

  it('不变量：正常输入的映射结果仍为完整可渲染 Session（title 非空、id 一致）', () => {
    for (const t of [
      { ...baseThread, title: null, intent: null },
      { ...baseThread, title: 'T' },
      { ...baseThread, intent: 'I' },
    ]) {
      const s: Session = mapThreadToSession(t)
      expect(s.id).toBe(t.thread_id)
      expect(s.title.length).toBeGreaterThan(0)
      expect(s.createdAt.length).toBeGreaterThan(0)
    }
  })
})

describe('mapThreadToSession 执行选项回显（metadata 兜底）', () => {
  it('后端无顶层字段时从 metadata 恢复 workspace/workspace_mode/isolation_mode', () => {
    // 事实契约：这些值创建时随前端写入 thread metadata，响应只有 metadata 键
    const s = mapThreadToSession({
      ...baseThread,
      metadata: {
        workspace: 'D:/proj/demo',
        workspace_mode: 'worktree',
        isolation_mode: 'isolated',
      },
    } as ThreadStateResponse)
    expect(s.workspace).toBe('D:/proj/demo')
    expect(s.workspaceMode).toBe('worktree')
    expect(s.isolationMode).toBe('isolated')
  })

  it('两源皆缺 → 三字段为 null（不伪造默认拓扑/隔离），有顶层字段时顶层优先', () => {
    const empty = mapThreadToSession({ ...baseThread, metadata: {} } as ThreadStateResponse)
    expect(empty.workspace).toBeNull()
    expect(empty.workspaceMode).toBeNull()
    expect(empty.isolationMode).toBeNull()

    const topFirst = mapThreadToSession({
      ...baseThread,
      workspace: 'D:/top/level',
      metadata: { workspace: 'D:/in/metadata' },
    } as ThreadStateResponse)
    expect(topFirst.workspace).toBe('D:/top/level')
  })

  it('isolation_mode 仅接受合法枚举语义（值原样透传，由 UI 层锁定选择集）', () => {
    const s = mapThreadToSession({
      ...baseThread,
      metadata: { isolation_mode: 'non_isolated' },
    } as ThreadStateResponse)
    expect(['isolated', 'non_isolated']).toContain(s.isolationMode)
  })
})
