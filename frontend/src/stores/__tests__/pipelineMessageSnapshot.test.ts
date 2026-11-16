// @feature: FP-T12 前端适配（pipelineMessageSnapshot 管道消息快照） | @ci: frontend-test
/**
 * buildPersistedSnapshot 快照构建测试
 *
 * 验证 persist partialize 抽件后的行为契约：
 * 1. 只落盘 7 个数据域，运行时态（streaming/压缩/停止/失败标记等）一律不进快照；
 * 2. 元数据/游标按保留管道集过滤（被裁剪管道不残留落盘）；
 * 3. 单管道条数上限经 trimMessagesForPersistence 生效。
 */
import { describe, it, expect } from 'vitest'
import {
  buildPersistedSnapshot,
  _PERSIST_LIMITS,
} from '@/stores/pipelineMessagePersistence'
import { PIPELINE_MESSAGE_ACCOUNT_RESET } from '@/stores/pipelineMessageState'
import type { PipelineMessageState } from '@/stores/pipelineMessageState'
import type { Message } from '@/types/models'

const makeMsg = (id: string, seq: number, ts: string, content = 'x'): Message => ({
  id,
  sessionId: 'sess',
  sequence: seq,
  role: 'assistant',
  content,
  timestamp: ts,
  parentId: null,
  status: 'completed',
})

const makeState = (over: Partial<PipelineMessageState> = {}): PipelineMessageState =>
  ({
    ...PIPELINE_MESSAGE_ACCOUNT_RESET,
    messagesByPipeline: {},
    pipelines: {},
    pipelineSessionMap: {},
    streamingState: {},
    compressingByPipeline: {},
    roundQueueDepthByPipeline: {},
    stoppingByPipeline: {},
    loadErrorByPipeline: {},
    topCursorsByPipeline: {},
    bottomCursorsByPipeline: {},
    hasMoreOlderByPipeline: {},
    isLoadingOlderByPipeline: {},
    reconciledByPipeline: {},
    spanIndex: {},
    segmentViewByPipeline: {},
    ...over,
  }) as PipelineMessageState

describe('buildPersistedSnapshot', () => {
  it('空状态：七个数据域全空、activePipelineId=null，运行时态字段不出现', () => {
    const snap = buildPersistedSnapshot(makeState())
    expect(Object.keys(snap).sort()).toEqual(
      [
        'activePipelineId',
        'bottomCursorsByPipeline',
        'hasMoreOlderByPipeline',
        'messagesByPipeline',
        'pipelineSessionMap',
        'pipelines',
        'topCursorsByPipeline',
      ].sort(),
    )
    expect(snap.messagesByPipeline).toEqual({})
    expect(snap.activePipelineId).toBeNull()
  })

  it('只落盘声明的七个数据域——运行时态（streaming/压缩/停止/失败标记）不进快照', () => {
    const state = makeState({
      streamingState: { p1: { isStreaming: true, messageId: 'm1' } },
      compressingByPipeline: { p1: true },
      stoppingByPipeline: { p1: true },
      loadErrorByPipeline: { p1: true },
      roundQueueDepthByPipeline: { p1: 3 },
    })
    const snap = buildPersistedSnapshot(state)
    expect('streamingState' in snap).toBe(false)
    expect('compressingByPipeline' in snap).toBe(false)
    expect('stoppingByPipeline' in snap).toBe(false)
    expect('loadErrorByPipeline' in snap).toBe(false)
    expect('roundQueueDepthByPipeline' in snap).toBe(false)
  })

  it('元数据/归属/游标按保留管道集过滤：无消息管道不残留落盘', () => {
    const snap = buildPersistedSnapshot(
      makeState({
        messagesByPipeline: { p1: [makeMsg('m1', 1, '2026-01-01T00:00:00Z')] },
        pipelines: {
          p1: { pipelineId: 'p1', sessionId: 's1', level: 1, tabId: null, agentName: 'a', status: 'idle', parentId: null, unreadCount: 0 },
          p2: { pipelineId: 'p2', sessionId: 's2', level: 1, tabId: null, agentName: 'b', status: 'idle', parentId: null, unreadCount: 5 },
        },
        pipelineSessionMap: { p1: 's1', p2: 's2' },
        topCursorsByPipeline: { p1: 1, p2: 9 },
        bottomCursorsByPipeline: { p1: 1, p2: 9 },
        hasMoreOlderByPipeline: { p1: true, p2: false },
        activePipelineId: 'p1',
      }),
    )
    expect(Object.keys(snap.pipelines)).toEqual(['p1'])
    expect(snap.pipelineSessionMap).toEqual({ p1: 's1' })
    expect(snap.topCursorsByPipeline).toEqual({ p1: 1 })
    expect(snap.bottomCursorsByPipeline).toEqual({ p1: 1 })
    expect(snap.hasMoreOlderByPipeline).toEqual({ p1: true })
    expect(snap.activePipelineId).toBe('p1')
  })

  it('单管道超限时经裁剪只保留最新 250 条（与 trimMessagesForPersistence 同策略）', () => {
    const LIMIT = _PERSIST_LIMITS.maxMessagesPerPipeline
    const msgs: Message[] = []
    for (let i = 1; i <= 300; i++) {
      msgs.push(makeMsg(`m${i}`, i, `2026-01-01T00:00:0${i % 10}Z`))
    }
    const snap = buildPersistedSnapshot(makeState({ messagesByPipeline: { p1: msgs } }))
    expect(snap.messagesByPipeline.p1).toHaveLength(LIMIT)
    expect(snap.messagesByPipeline.p1[0].sequence).toBe(300 - LIMIT + 1)
  })

  it('pickByKey 防御分支：字段缺席（undefined rec）落空对象不抛', () => {
    const partial = {
      ...makeState({
        messagesByPipeline: { p1: [makeMsg('m1', 1, '2026-01-01T00:00:00Z')] },
        pipelines: {
          p1: { pipelineId: 'p1', sessionId: 's1', level: 1, tabId: null, agentName: 'a', status: 'idle', parentId: null, unreadCount: 0 },
        },
      }),
    }
    delete (partial as Record<string, unknown>).pipelineSessionMap
    const snap = buildPersistedSnapshot(partial as unknown as PipelineMessageState)
    expect(snap.pipelineSessionMap).toEqual({})
    expect(Object.keys(snap.pipelines)).toEqual(['p1'])
  })
})
