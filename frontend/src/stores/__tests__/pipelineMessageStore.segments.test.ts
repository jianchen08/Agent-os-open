// @feature: FP-T12 前端适配(段索引store) | @ci: frontend-test
// @feature: message-segments P1 | @ci: frontend-test
/**
 * pipelineMessageStore 段索引（消息段模型：‹i/n› 多代切换与压缩原文）。
 *
 * 契约（[来源: docs/working/消息历史双能力方案_多代切换与压缩原文_20260923.md §2.5/§5]）：
 * - spanIndex：pipelineId → 段元数据清单，打开会话时拉一次（loadPipelineMessages）；
 * - getSegmentsAt：某 base_seq 的段列表，多段同 base_seq 按 created_at 升序（代号 = 序数）；
 * - applySegmentView：后缀语义乐观换装——保留 base_seq 之前消息，其后整段呈现为段成员；
 * - fetchSegments 失败降级为空清单（切换器隐藏），不影响消息主链路。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import type * as pipelineMessageStoreMod from '@/stores/pipelineMessageStore'
import type { SegmentDetail, SegmentMeta } from '@/services/api/messageSegments'
import type { Message } from '@/types/models'

vi.mock('@/utils/logger', async () => {
  const { loggerMockFull } = await import('./helpers/storeTestMocks')
  return loggerMockFull()
})

const getMessageSegmentsMock = vi.fn()
vi.mock('@/services/api/messageSegments', () => ({
  getMessageSegments: (...args: unknown[]) => getMessageSegmentsMock(...args),
}))

const getMessagesApiMock = vi.fn()
vi.mock('@/services/api/session', async (importOriginal) => {
  const actual = await importOriginal<Record<string, unknown>>()
  return {
    ...actual,
    getMessages: (...args: unknown[]) => getMessagesApiMock(...args),
  }
})

/** 段元数据工厂：created_at 缺省按序号生成，保证可区分排序 */
function makeSegment(id: string, base_seq: number, created_at?: string): SegmentMeta {
  return {
    id,
    base_seq,
    base_len: 3,
    visible_to: '',
    preview: `预览 ${id}`,
    created_at: created_at ?? new Date(Date.parse('2026-09-23T00:00:00Z') + base_seq * 1000).toISOString(),
  }
}

describe('pipelineMessageStore 段索引', () => {
  let usePipelineMessageStore: pipelineMessageStoreMod.usePipelineMessageStore

  beforeEach(async () => {
    vi.resetModules()
    vi.clearAllMocks()
    const { resetPipelineStoreState } = await import('./helpers/storeTestMocks')
    usePipelineMessageStore = await resetPipelineStoreState({ pipelineSessionMap: { 'pipe-1': 'sess-1' } })
  })

  it('fetchSegments 写入 spanIndex 并按 (base_seq, created_at) 升序排列', async () => {
    const segLate = makeSegment('seg-late', 50, '2026-09-23T02:00:00Z')
    const segOther = makeSegment('seg-other', 60, '2026-09-23T01:00:00Z')
    const segEarly = makeSegment('seg-early', 50, '2026-09-23T00:00:00Z')
    getMessageSegmentsMock.mockResolvedValue([segLate, segOther, segEarly])

    await usePipelineMessageStore.getState().fetchSegments('pipe-1')

    expect(usePipelineMessageStore.getState().spanIndex['pipe-1']).toEqual([
      segEarly,
      segLate,
      segOther,
    ])
  })

  it('getSegmentsAt 只取该锚点并按 created_at 升序（多代代号 = 序数）', async () => {
    getMessageSegmentsMock.mockResolvedValue([
      makeSegment('seg-gen2', 50, '2026-09-23T02:00:00Z'),
      makeSegment('seg-other-anchor', 66, '2026-09-23T03:00:00Z'),
      makeSegment('seg-gen1', 50, '2026-09-23T00:00:00Z'),
    ])
    await usePipelineMessageStore.getState().fetchSegments('pipe-1')

    const gens = usePipelineMessageStore.getState().getSegmentsAt('pipe-1', 50)
    expect(gens.map((s) => s.id)).toEqual(['seg-gen1', 'seg-gen2'])
    // 无段的锚点返回空列表；未知管道同此
    expect(usePipelineMessageStore.getState().getSegmentsAt('pipe-1', 999)).toEqual([])
    expect(usePipelineMessageStore.getState().getSegmentsAt('pipe-none', 50)).toEqual([])
    expect(getMessageSegmentsMock).toHaveBeenCalledTimes(1)
  })

  it('fetchSegments 失败降级为空清单且不上抛（切换器隐藏，主链路不受影响）', async () => {
    getMessageSegmentsMock.mockRejectedValue(new Error('network down'))
    await expect(usePipelineMessageStore.getState().fetchSegments('pipe-1')).resolves.toBeUndefined()
    expect(usePipelineMessageStore.getState().spanIndex['pipe-1']).toEqual([])
  })

  it('loadPipelineMessages 打开会话时拉一次段清单；spanIndex 已有清单不重拉', async () => {
    getMessagesApiMock.mockResolvedValue({ messages: [], total: 0, session_id: 'sess-1' })
    getMessageSegmentsMock.mockResolvedValue([makeSegment('seg-1', 50)])

    await usePipelineMessageStore.getState().loadPipelineMessages('pipe-1', { threadId: 'sess-1', mode: 'init' })
    expect(getMessageSegmentsMock).toHaveBeenCalledTimes(1)

    // 第二次进入（spanIndex 已有该管道条目）不再重拉
    await usePipelineMessageStore.getState().loadPipelineMessages('pipe-1', { threadId: 'sess-1', mode: 'auto' })
    expect(getMessageSegmentsMock).toHaveBeenCalledTimes(1)
  })

  describe('applySegmentView 乐观换装（后缀语义 §2.5）', () => {
    const detail: SegmentDetail = {
      ...makeSegment('seg-gen1', 50, '2026-09-23T00:00:00Z'),
      members: [
        {
          id: 'mc-user-v1',
          thread_id: 'sess-1',
          sequence: 50,
          role: 'user',
          content: '第一代问题',
          timestamp: '2026-09-23T00:00:01Z',
          status: 'completed',
        },
        {
          id: 'mc-assistant-v1',
          thread_id: 'sess-1',
          role: 'assistant',
          content: '第一代回答',
          timestamp: '2026-09-23T00:00:02Z',
          status: 'completed',
        },
      ],
    }

    function seedLive() {
      const mk = (id: string, seq: number, role: Message['role'], content: string): Message =>
        ({
          id,
          sessionId: 'sess-1',
          sequence: seq,
          role,
          content,
          timestamp: new Date(Date.parse('2026-09-23T00:00:00Z') + seq * 1000).toISOString(),
          parentId: null,
          status: 'completed',
        }) as Message
      usePipelineMessageStore.setState((s) => ({
        messagesByPipeline: {
          ...s.messagesByPipeline,
          'pipe-1': [
            mk('mc-keep', 49, 'user', '锚点之前保留'),
            mk('mc-live-user', 50, 'user', '第二代问题'),
            mk('mc-live-assistant', 51, 'assistant', '第二代回答'),
          ],
        },
      }))
    }

    it('保留 base_seq 之前的消息，其后整段换装为段成员（缺 seq 成员按起点补位）', () => {
      seedLive()
      usePipelineMessageStore.getState().applySegmentView('pipe-1', detail, 'sess-1')

      const msgs = usePipelineMessageStore.getState().getMessages('pipe-1')
      expect(msgs.map((m) => m.id)).toEqual(['mc-keep', 'mc-user-v1', 'mc-assistant-v1'])
      // 锚点之前不动
      expect(msgs[0]?.content).toBe('锚点之前保留')
      // 段成员权威 seq 保留 / 缺失按 base_seq + 序数补位
      expect(msgs[1]?.sequence).toBe(50)
      expect(msgs[2]?.sequence).toBe(51)
      expect(msgs[2]?.content).toBe('第一代回答')
    })

    it('换装记录段视图标记；clearSegmentView 清除（幂等）', () => {
      seedLive()
      const ps = usePipelineMessageStore.getState()
      ps.applySegmentView('pipe-1', detail, 'sess-1')
      expect(usePipelineMessageStore.getState().segmentViewByPipeline['pipe-1']).toEqual({
        baseSeq: 50,
        segmentId: 'seg-gen1',
      })

      ps.clearSegmentView('pipe-1')
      expect(usePipelineMessageStore.getState().segmentViewByPipeline['pipe-1']).toBeUndefined()
      // 幂等：再清不抛不建
      ps.clearSegmentView('pipe-1')
      expect(usePipelineMessageStore.getState().segmentViewByPipeline['pipe-1']).toBeUndefined()
    })

    it('泛化性：另一锚点 base_seq=66 的段同样按其起点换装（不硬编码 50）', () => {
      seedLive()
      const detail66: SegmentDetail = {
        ...makeSegment('seg-at-66', 66, '2026-09-23T00:00:00Z'),
        members: [
          {
            id: 'mc-tail',
            thread_id: 'sess-1',
            sequence: 66,
            role: 'user',
            content: '锚点66的一代',
            timestamp: '2026-09-23T00:00:01Z',
            status: 'completed',
          },
        ],
      }
      usePipelineMessageStore.setState((s) => ({
        messagesByPipeline: {
          ...s.messagesByPipeline,
          'pipe-1': [
            ...(s.messagesByPipeline['pipe-1'] || []),
            { id: 'mc-66-live', sessionId: 'sess-1', sequence: 66, role: 'user', content: '锚点66活跃', timestamp: '2026-09-23T00:05:00Z', parentId: null, status: 'completed' } as Message,
          ],
        },
      }))
      usePipelineMessageStore.getState().applySegmentView('pipe-1', detail66, 'sess-1')

      const msgs = usePipelineMessageStore.getState().getMessages('pipe-1')
      // seq < 66 全保留（含 49/50/51），其后换成段成员
      expect(msgs.map((m) => m.id)).toEqual(['mc-keep', 'mc-live-user', 'mc-live-assistant', 'mc-tail'])
      expect(usePipelineMessageStore.getState().segmentViewByPipeline['pipe-1']?.baseSeq).toBe(66)
    })
  })
})
