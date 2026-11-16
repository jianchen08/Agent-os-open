/**
 * 会话最近活跃排序单点（R10 多端统一）回归锁
 *
 * 锁定契约（2026-10-14 用户裁定）：
 * - 会话列表按最近活跃（updatedAt 优先、createdAt 兜底）降序，最近使用自然置顶
 * - 置顶/星标只作视觉标记，不参与排序
 * - 排序返回新数组，不 mutate 入参（query cache 数据安全）
 *
 * 防拟合：多组有区分度输入（正常/边界/时序颠倒）+ 性质断言
 * （单调性/幂等/乱序输入同结果）。
 */
import { describe, expect, it, vi } from 'vitest'
import {
  compareSessionRecency,
  sessionRecencyMs,
  sortSessionsByRecency,
} from '../sessionListStore'
import type { Session } from '@/types/models'

// 纯函数测试不触 store 动作，但模块导入链含重依赖（WS/api），与同族测试同款替换
vi.mock('@/services/api/session', () => ({
  getSessions: vi.fn(),
  createSession: vi.fn(),
  deleteSession: vi.fn(),
  updateSessionAgent: vi.fn(),
  updateSession: vi.fn(),
  mergeConsecutiveAssistantMessages: (m: unknown[]) => m,
}))
vi.mock('@/services/websocket/GlobalWebSocket', () => ({
  globalWS: { sendCancel: vi.fn() },
}))
vi.mock('../agentTabStore', () => ({
  useAgentTabStore: {
    getState: () => ({
      currentSessionId: null,
      initSessionTabs: vi.fn(),
      resetAllTabs: vi.fn(),
    }),
  },
}))

function makeSession(overrides: Partial<Session> = {}): Session {
  return {
    id: 's',
    title: '会话',
    createdAt: '2026-01-01T00:00:00.000Z',
    updatedAt: '2026-01-01T00:00:00.000Z',
    pipelineIds: [],
    ...overrides,
  } as Session
}

/** 任意顺序输入下排序结果都应等于按时间降序（性质：乱序不变量） */
const TIMES = [
  '2026-01-01T00:00:00.000Z',
  '2026-06-15T12:30:00.000Z',
  '2025-12-31T23:59:59.000Z',
  '2026-03-08T08:00:00.000Z',
]

describe('sortSessionsByRecency（R10 共享层单点排序）', () => {
  it('按 updatedAt 降序：最近活跃在前（单调性）', () => {
    const sorted = sortSessionsByRecency(
      TIMES.map((t, i) => makeSession({ id: `s${i}`, updatedAt: t })),
    )
    const ms = sorted.map(sessionRecencyMs)
    for (let i = 1; i < ms.length; i++) {
      expect(ms[i - 1]).toBeGreaterThanOrEqual(ms[i])
    }
    expect(sorted[0].updatedAt).toBe('2026-06-15T12:30:00.000Z')
    expect(sorted[sorted.length - 1].updatedAt).toBe('2025-12-31T23:59:59.000Z')
  })

  it('updatedAt 缺失回退 createdAt（列表数据里实际可用的最新时间戳）', () => {
    const noUpdate = makeSession({ id: 'a', createdAt: '2026-05-01T00:00:00.000Z', updatedAt: undefined })
    const olderUpdate = makeSession({ id: 'b', updatedAt: '2026-01-01T00:00:00.000Z' })
    const sorted = sortSessionsByRecency([olderUpdate, noUpdate])
    expect(sorted[0].id).toBe('a')
    expect(sorted[1].id).toBe('b')
  })

  it('置顶/星标不参与排序：老置顶排在星标普通会话之后', () => {
    const oldPinned = makeSession({
      id: 'pinned-old',
      pinned: true,
      updatedAt: '2026-01-01T00:00:00.000Z',
    })
    const starredMid = makeSession({
      id: 'starred-mid',
      starred: true,
      updatedAt: '2026-03-01T00:00:00.000Z',
    })
    const plainNew = makeSession({ id: 'plain-new', updatedAt: '2026-06-01T00:00:00.000Z' })
    const sorted = sortSessionsByRecency([oldPinned, starredMid, plainNew])
    expect(sorted.map((s) => s.id)).toEqual(['plain-new', 'starred-mid', 'pinned-old'])
  })

  it('时间戳相同（并列）保持传入顺序（稳定排序，幂等）', () => {
    const a = makeSession({ id: 'a', updatedAt: '2026-02-01T00:00:00.000Z' })
    const b = makeSession({ id: 'b', updatedAt: '2026-02-01T00:00:00.000Z' })
    const once = sortSessionsByRecency([a, b])
    const twice = sortSessionsByRecency(once)
    expect(once.map((s) => s.id)).toEqual(['a', 'b'])
    expect(twice.map((s) => s.id)).toEqual(['a', 'b'])
  })

  it('非法时间戳按最旧处理，不抛错不产生 NaN 序', () => {
    const broken = makeSession({ id: 'broken', updatedAt: 'not-a-date' })
    const valid = makeSession({ id: 'valid', updatedAt: '2026-01-02T00:00:00.000Z' })
    const sorted = sortSessionsByRecency([broken, valid])
    expect(sorted.map((s) => s.id)).toEqual(['valid', 'broken'])
    expect(compareSessionRecency(broken, valid)).toBeGreaterThan(0)
  })

  it('不 mutate 入参数组（query cache 写安全）', () => {
    const input = [
      makeSession({ id: 'old', updatedAt: '2026-01-01T00:00:00.000Z' }),
      makeSession({ id: 'new', updatedAt: '2026-06-01T00:00:00.000Z' }),
    ]
    const snapshot = input.map((s) => s.id)
    sortSessionsByRecency(input)
    expect(input.map((s) => s.id)).toEqual(snapshot)
  })

  it('空列表返回空数组', () => {
    expect(sortSessionsByRecency([])).toEqual([])
  })
})
