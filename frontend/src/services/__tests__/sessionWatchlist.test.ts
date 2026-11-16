/** @feature FP-0.2.九 会话工作空间与会话列表（新建会话窗口外可见性补偿） @vision V6 可即用 @ci frontend-test */
/**
 * sessionWatchlist 契约测试
 *
 * 根因背景：服务端会话列表端点固定按 updated_at DESC 截断 100 条（无分页、
 * 无按 id 读取），新建后零活动的会话会被持续活跃的其他会话挤出窗口，侧栏
 * 列表/搜索命中映射/会话树分组随之全部不可见（真机缺陷：新建会话三重不可见）。
 *
 * 契约：
 * - rememberSession 登记新建会话，同 id 幂等覆盖，超上限丢最旧；
 * - forgetSession 注销登记（删除后不得被合并复活）；
 * - mergeSessionsWithWatchlist：窗口外登记项补入列表尾部；窗口内以服务端
 *   权威值为准并回写登记快照；无窗口外项且无变更时原引用返回（query 订阅
 *   方不无效重渲染）；损坏载荷按空表处理。
 */

import { beforeEach, describe, expect, it } from 'vitest'
import {
  forgetSession,
  mergeSessionsWithWatchlist,
  rememberSession,
} from '@/services/sessionWatchlist'
import { uiStorage } from '@/utils/storage'
import type { Session } from '@/types/models'

function makeSession(id: string, overrides: Partial<Session> = {}): Session {
  return {
    id,
    title: `会话-${id}`,
    createdAt: '2026-10-12T08:00:00Z',
    updatedAt: '2026-10-12T08:00:00Z',
    metadata: { session_type: 'main_pipeline' },
    agentId: null,
    workspace: null,
    workspaceMode: null,
    isolationMode: null,
    pipelineIds: [`pipe-${id}`],
    pinned: false,
    starred: false,
    ...overrides,
  }
}

function storedList(): Session[] {
  return (uiStorage.getSessionWatchlist() as Session[] | null) ?? []
}

beforeEach(() => {
  uiStorage.setSessionWatchlist([])
})

describe('rememberSession', () => {
  it('登记新建会话：合并时窗口外项补入列表尾部', () => {
    const created = makeSession('thread-new', { workspace: 'D:/proj-a' })
    rememberSession(created)

    const fetched = [makeSession('thread-old-1'), makeSession('thread-old-2')]
    const merged = mergeSessionsWithWatchlist(fetched)

    expect(merged.map((s) => s.id)).toContain('thread-new')
    expect(merged.filter((s) => s.id === 'thread-new')).toHaveLength(1)
    // 窗口外项保留登记快照的全部字段（workspace 供会话树分组归属）
    expect(merged.find((s) => s.id === 'thread-new')?.workspace).toBe('D:/proj-a')
  })

  it('同 id 幂等覆盖：重复登记不产生重复条目', () => {
    rememberSession(makeSession('thread-1', { title: '初稿' }))
    rememberSession(makeSession('thread-1', { title: '定稿' }))

    const merged = mergeSessionsWithWatchlist([])
    expect(merged).toHaveLength(1)
    expect(merged[0]?.title).toBe('定稿')
  })

  it('有界：超过 50 条丢最旧登记', () => {
    for (let i = 0; i < 52; i++) {
      rememberSession(makeSession(`thread-${i}`))
    }
    const list = storedList()
    expect(list).toHaveLength(50)
    // 最旧两条被淘汰，最新保留
    expect(list.map((s) => s.id)).not.toContain('thread-0')
    expect(list.map((s) => s.id)).not.toContain('thread-1')
    expect(list.map((s) => s.id)).toContain('thread-51')
  })
})

describe('forgetSession', () => {
  it('注销后合并不再补回（删除后不得复活）', () => {
    rememberSession(makeSession('thread-gone'))
    forgetSession('thread-gone')

    expect(mergeSessionsWithWatchlist([])).toHaveLength(0)
  })

  it('注销不存在的 id 零动作（不误写存储）', () => {
    rememberSession(makeSession('thread-keep'))
    forgetSession('thread-absent')

    expect(storedList().map((s) => s.id)).toEqual(['thread-keep'])
  })
})

describe('mergeSessionsWithWatchlist', () => {
  it('窗口内命中：以服务端权威值并入结果，且回写登记快照（窗口内编辑不丢）', () => {
    rememberSession(makeSession('thread-1', { title: '登记时的旧标题' }))
    const serverCopy = makeSession('thread-1', { title: '服务端新标题' })

    const merged = mergeSessionsWithWatchlist([serverCopy])

    expect(merged).toHaveLength(1)
    expect(merged[0]?.title).toBe('服务端新标题')
    // 登记快照被服务端权威值刷新
    expect(storedList()[0]?.title).toBe('服务端新标题')
  })

  it('无窗口外项且无回写变更：原引用返回（不触发无效重渲染）', () => {
    const fetched = [makeSession('thread-a'), makeSession('thread-b')]
    expect(mergeSessionsWithWatchlist(fetched)).toBe(fetched)

    // 登记项全部在窗口内且快照即服务端对象（同引用零变更）：仍原引用返回
    const shared = makeSession('thread-shared')
    rememberSession(shared)
    const fetchedWithSame = [shared, makeSession('thread-d')]
    expect(mergeSessionsWithWatchlist(fetchedWithSame)).toBe(fetchedWithSame)
  })

  it('混合场景：窗口内 + 窗口外并存，两侧都保留', () => {
    const serverFresh = makeSession('thread-in', { title: '窗口内' })
    rememberSession(makeSession('thread-in', { title: '登记旧值' }))
    rememberSession(makeSession('thread-out', { title: '窗口外' }))

    const merged = mergeSessionsWithWatchlist([serverFresh, makeSession('thread-other')])

    expect(merged).toHaveLength(3)
    expect(merged.find((s) => s.id === 'thread-in')?.title).toBe('窗口内')
    expect(merged.find((s) => s.id === 'thread-out')?.title).toBe('窗口外')
  })

  it('存储载荷损坏按空表处理（不抛错、不污染结果）', () => {
    uiStorage.setSessionWatchlist('not-a-list' as unknown as unknown[])
    // 非数组载荷 → 读侧 null → 空表
    const fetched = [makeSession('thread-a')]
    expect(mergeSessionsWithWatchlist(fetched)).toBe(fetched)
  })

  it('登记项缺 id 的脏数据被过滤（不产生无法点击的幽灵条目）', () => {
    uiStorage.setSessionWatchlist([
      { title: '无 id 脏数据' },
      makeSession('thread-ok'),
    ] as unknown as unknown[])
    const merged = mergeSessionsWithWatchlist([])
    expect(merged.map((s) => s.id)).toEqual(['thread-ok'])
  })
})
