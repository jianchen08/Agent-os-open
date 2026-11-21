// @feature: FP-0.2.流式活性统一 | @ci: frontend-test
/**
 * 流式活动状态如实化回归（任务 3a19e4183b20 / 用户裁定 2026-11-18）
 *
 * 不变量：活动指示必须如实反映实时状态，终态事件到达前不得变完成态。
 *
 * - 缺陷①：thinking part 已置 done 后仍有增量到达 → 必须恢复 streaming
 *   （否则「内容继续增长 + 卡片显示已完成」）。
 * - 缺陷②：finalizeMessage 不得把无结果 tool_call 强制置 done（终态只由
 *   tool_result 写入）；有结果/错误时才收口。
 * - 派生：无结果 + 流式上下文 = running（转圈持续）；非流式才 interrupted。
 *
 * mock 边界仅限网络（apiClient）与持久化（indexedDbStorage）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { finalizePartsForInterrupt } from '@/stores/pipelineMessageStore'
import { deriveToolCallActivityStatus } from '@/types/activity'
import type * as pipelineMessageStoreMod from '@/stores/pipelineMessageStore'
import type { MessagePart } from '@/types/messageParts'
import type { Message } from '@/types/models'

vi.mock('@/utils/logger', () => ({
  loggers: {
    sessionStore: { debug: vi.fn(), info: vi.fn(), warn: vi.fn(), error: vi.fn() },
  },
}))

const { mockGet } = vi.hoisted(() => ({ mockGet: vi.fn() }))
vi.mock('@/services/api/client', () => ({ default: { get: mockGet } }))

vi.mock('@/services/api/messageSegments', () => ({
  getMessageSegments: vi.fn(async () => []),
}))

const { storageGet, storageSet, storageRemove } = vi.hoisted(() => ({
  storageGet: vi.fn<() => Promise<unknown>>(),
  storageSet: vi.fn<() => Promise<void>>(),
  storageRemove: vi.fn<() => Promise<void>>(),
}))
vi.mock('@/utils/indexedDbStorage', () => ({
  indexedDbStorage: {
    getItem: storageGet,
    setItem: storageSet,
    removeItem: storageRemove,
  },
  createDeferredIndexedDbStorage: () => ({
    getItem: storageGet,
    setItem: storageSet,
    removeItem: storageRemove,
  }),
}))

const PIPE = 'pipe-stream-invariant-001'
const MID = 'msg-stream-invariant-001'
const THREAD = 'thread-stream-invariant-001'

type Store = typeof pipelineMessageStoreMod.usePipelineMessageStore

describe('流式活动状态如实化', () => {
  let store: Store

  beforeEach(async () => {
    const mod = await import('@/stores/pipelineMessageStore')
    store = mod.usePipelineMessageStore
    store.setState({ messagesByPipeline: {} })
  })

  function seed(parts: Message['parts'], status: Message['status'] = 'streaming') {
    store.setState({
      messagesByPipeline: {
        [PIPE]: [
          {
            id: MID,
            sessionId: THREAD,
            role: 'assistant',
            content: '',
            timestamp: new Date().toISOString(),
            status,
            parts,
          } as Message,
        ],
      },
    })
  }

  function currentPart(index = 0) {
    const msgs = store.getState().getMessages(PIPE)
    return msgs.find((m) => m.id === MID)?.parts?.[index]
  }

  it('缺陷①：done 的思考 part 收到增量后恢复 streaming 并继续增长', () => {
    seed([{ type: 'thinking', content: '已闭合的思考', state: 'done' }])

    store.getState().appendToPart(PIPE, MID, 0, '迟到增量')

    const part = currentPart()
    expect(part?.type).toBe('thinking')
    expect((part as { content: string }).content).toBe('已闭合的思考迟到增量')
    expect(part?.state).toBe('streaming')
  })

  it('缺陷①对照：仍在 streaming 的 part 追加后保持 streaming（无抖动）', () => {
    seed([{ type: 'thinking', content: '进行中', state: 'streaming' }])

    store.getState().appendToPart(PIPE, MID, 0, '更多')

    expect(currentPart()?.state).toBe('streaming')
    expect((currentPart() as { content: string }).content).toBe('进行中更多')
  })

  it('缺陷②：finalizeMessage 不把无结果工具卡收口为 done', () => {
    seed([{ type: 'tool_call', callId: 'call_wait', name: 'file_read', args: {}, state: 'calling' }])

    store.getState().finalizeMessage(PIPE, MID)

    const part = currentPart()
    expect(part?.type).toBe('tool_call')
    expect(part?.state).not.toBe('done')
    // 消息本身仍收尾为 completed（轮级终态），只是工具卡保留在途判定
    expect(store.getState().getMessages(PIPE)[0]?.status).toBe('completed')
  })

  it('缺陷②对照：有结果的工具卡在 finalizeMessage 时收口为 done', () => {
    seed([
      { type: 'tool_call', callId: 'call_done', name: 'file_read', args: {}, state: 'calling', result: 'ok' },
    ])

    store.getState().finalizeMessage(PIPE, MID)

    expect(currentPart()?.state).toBe('done')
  })

  it('缺陷②对照：有错误的工具卡在 finalizeMessage 时保持 error', () => {
    seed([
      { type: 'tool_call', callId: 'call_err', name: 'file_read', args: {}, state: 'error', error: 'boom' },
    ])

    store.getState().finalizeMessage(PIPE, MID)

    expect(currentPart()?.state).toBe('error')
  })

  it('中断收口映射（finalizePartsForInterrupt）：在途 part 全部离开 streaming', () => {
    // Must Fix #2 的核心守卫：中断路径（stopStreaming / F2 宽限复查）必须同刀收口
    // 残留 part，否则 thinking 恒 streaming → 思考卡永续转圈。
    const mapped = finalizePartsForInterrupt([
      { type: 'thinking', content: '半截思考', state: 'streaming' } as MessagePart,
      { type: 'text', content: '半截正文', state: 'streaming' } as MessagePart,
      { type: 'tool_call', callId: 'c_run', name: 'file_read', args: {}, state: 'calling' } as MessagePart,
      { type: 'tool_call', callId: 'c_str', name: 'file_read', args: {}, state: 'streaming' } as MessagePart,
      { type: 'tool_call', callId: 'c_done', name: 'file_read', args: {}, state: 'done', result: 'ok' } as MessagePart,
      { type: 'system', content: '通知', level: 'info', notificationType: 'x' } as MessagePart,
    ])

    expect(mapped[0].state).toBe('done')
    expect(mapped[1].state).toBe('done')
    expect(mapped[2].state).toBe('error')
    expect(mapped[3].state).toBe('error')
    // 已终态的 part 不得被改写（幂等面）
    expect(mapped[4].state).toBe('done')
    expect(mapped[5].type).toBe('system')
    // 性质断言：映射后不存在任何 streaming/calling（思考卡不可能再被驱动转圈）
    expect(
      mapped.some((p) => {
        const st = (p as { state?: string }).state
        return st === 'streaming' || st === 'calling'
      }),
    ).toBe(false)
  })

  it('派生：无结果 + 流式上下文 = running；无结果 + 非流式 = interrupted', () => {
    const evidence = { state: 'calling', hasResult: false, hasError: false }
    expect(deriveToolCallActivityStatus(evidence, true)).toBe('running')
    expect(deriveToolCallActivityStatus(evidence, false)).toBe('interrupted')
    // 性质断言：结果到达即 completed，与上下文无关
    expect(deriveToolCallActivityStatus({ ...evidence, hasResult: true }, true)).toBe('completed')
    expect(deriveToolCallActivityStatus({ ...evidence, hasResult: true }, false)).toBe('completed')
  })
})
