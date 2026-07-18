/** 回归测试：appendMessages / prependMessages 与 initFromAPI 共用 mergeApiWithExisting，
 *  含 clientMessageId 对账。修复前 appendMessages 只按 id 去重，乐观 user（id=UUID）与
 *  API user（id=record_id）clientMessageId 相同但 id 不同 → 切会话回来两条并存。
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { usePipelineMessageStore } from '../pipelineMessageStore'
import type { Message } from '@/types/models'

const PIPELINE_ID = 'dfc321a22cd4'
const SESSION_ID = '6c9abaf29fe1'

function makeMsg(id: string, sequence: number, overrides: Partial<Message> = {}): Message {
  return {
    id,
    sessionId: SESSION_ID,
    role: 'assistant',
    content: '',
    timestamp: new Date(Date.now() + sequence * 100).toISOString(),
    sequence,
    parentId: null,
    status: 'completed',
    ...overrides,
  } as Message
}

describe('appendMessages / prependMessages 的 clientMessageId 对账', () => {
  beforeEach(() => {
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: {},
      streamingState: {},
      activePipelineId: null,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
      reconciledByPipeline: {},
    })
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID,
      sessionId: SESSION_ID,
      level: 1,
      tabId: null,
      agentName: '',
      status: 'idle',
      parentId: null,
      unreadCount: 0,
    } as any)
  })

  it('场景1: 乐观 user（UUID id）+ appendMessages 推回 API user（record_id，同 clientMessageId）→ user 仅一条', () => {
    const store = usePipelineMessageStore.getState()

    // 本地已有：历史 AI + 乐观 user（前端 UUID id，clientMessageId=同 UUID）
    const CLIENT_UUID = 'c78961a4-0aaa-4bbb-8ccc-dddddddddddd'
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('ai-1-hex', 1, { role: 'assistant', content: 'ai1' }),
      makeMsg('user-1-hex', 2, { role: 'user', content: 'u1' }),
      makeMsg('ai-2-hex', 3, { role: 'assistant', content: 'ai2' }),
    ])
    store.addMessage(PIPELINE_ID, makeMsg(CLIENT_UUID, 4, {
      role: 'user',
      content: 'u2',
      clientMessageId: CLIENT_UUID,
    }))

    // 切会话回来：appendMessages 增量补漏，API 推回 user2（record_id，同 clientMessageId）+ ai3
    store.appendMessages(PIPELINE_ID, [
      makeMsg('record-user-2', 4, { role: 'user', content: 'u2', clientMessageId: CLIENT_UUID }),
      makeMsg('ai-3-hex', 5, { role: 'assistant', content: 'ai3' }),
    ])

    const msgs = store.getMessages(PIPELINE_ID)
    const u2Msgs = msgs.filter((m) => m.role === 'user' && m.content === 'u2')
    // 修复前：u2 有两条（UUID + record_id）；修复后：仅 API 版 record-user-2
    expect(u2Msgs).toHaveLength(1)
    expect(u2Msgs[0].id).toBe('record-user-2')
  })

  it('场景2（对齐断言）: appendMessages 终态 == initFromAPI 终态（user id 都是 record_id）', () => {
    // 两个独立 pipeline，相同初始状态，分别走 appendMessages 与 initFromAPI
    const PID_APPEND = 'pipe-append-align'
    const PID_INIT = 'pipe-init-align'
    const CLIENT_UUID = 'uuid-align-test-0001'
    for (const pid of [PID_APPEND, PID_INIT]) {
      usePipelineMessageStore.getState().registerPipeline({
        pipelineId: pid, sessionId: SESSION_ID, level: 1, tabId: null,
        agentName: '', status: 'idle', parentId: null, unreadCount: 0,
      } as any)
      usePipelineMessageStore.getState().initFromAPI(pid, [
        makeMsg('ai-1-hex', 1, { role: 'assistant', content: 'ai1' }),
        makeMsg('user-1-hex', 2, { role: 'user', content: 'u1' }),
      ])
      usePipelineMessageStore.getState().addMessage(pid, makeMsg(CLIENT_UUID, 3, {
        role: 'user', content: 'u2', clientMessageId: CLIENT_UUID,
      }))
    }

    // append 路径：增量补漏推回 API user2（record_id）
    usePipelineMessageStore.getState().appendMessages(PID_APPEND, [
      makeMsg('record-user-2-align', 3, { role: 'user', content: 'u2', clientMessageId: CLIENT_UUID }),
    ])
    // init 路径：全量（含历史 + 新 user2）
    usePipelineMessageStore.getState().initFromAPI(PID_INIT, [
      makeMsg('ai-1-hex', 1, { role: 'assistant', content: 'ai1' }),
      makeMsg('user-1-hex', 2, { role: 'user', content: 'u1' }),
      makeMsg('record-user-2-align', 3, { role: 'user', content: 'u2', clientMessageId: CLIENT_UUID }),
    ])

    const appendMsgs = usePipelineMessageStore.getState().getMessages(PID_APPEND)
    const initMsgs = usePipelineMessageStore.getState().getMessages(PID_INIT)
    const appendU2 = appendMsgs.find((m) => m.content === 'u2')
    const initU2 = initMsgs.find((m) => m.content === 'u2')

    // 两条路径的 user2 终态 id 相同（都是 API record_id，非乐观 UUID）
    expect(appendU2?.id).toBe('record-user-2-align')
    expect(initU2?.id).toBe('record-user-2-align')
    // append 路径 user2 不残留乐观 UUID 版
    expect(appendMsgs.find((m) => m.id === CLIENT_UUID)).toBeUndefined()
  })

  it('场景3: AI 消息 id 一致（WS hex == API record_id）→ 按 id 去重，不重复', () => {
    const store = usePipelineMessageStore.getState()
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('ai-1-hex', 1, { role: 'assistant', content: 'ai1' }),
    ])
    // 本地已有 AI（id 与后端一致）
    store.addMessage(PIPELINE_ID, makeMsg('fda41b5f24e6', 2, {
      role: 'assistant', content: 'ai2 reply',
      parts: [{ type: 'text', content: 'ai2 reply', sequence: 1, state: 'done' }] as any,
    }))

    store.appendMessages(PIPELINE_ID, [
      makeMsg('fda41b5f24e6', 2, { role: 'assistant', content: 'ai2 reply' }),
    ])

    const ai2Msgs = store.getMessages(PIPELINE_ID).filter((m) => m.content === 'ai2 reply')
    expect(ai2Msgs).toHaveLength(1)
  })

  it('场景4: 无 clientMessageId 的 user 消息，按 id 去重命中', () => {
    const store = usePipelineMessageStore.getState()
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('user-same-id', 1, { role: 'user', content: 'hi' }),
    ])

    // append 推回同 id user（无 clientMessageId）
    store.appendMessages(PIPELINE_ID, [
      makeMsg('user-same-id', 1, { role: 'user', content: 'hi' }),
    ])

    const userMsgs = store.getMessages(PIPELINE_ID).filter((m) => m.role === 'user' && m.content === 'hi')
    expect(userMsgs).toHaveLength(1)
  })

  it('场景5: prependMessages 同样按 clientMessageId 对账', () => {
    const store = usePipelineMessageStore.getState()
    const CLIENT_UUID = 'uuid-prepend-0001'

    // 本地：乐观 user（UUID id）+ 后续 AI
    store.addMessage(PIPELINE_ID, makeMsg(CLIENT_UUID, 5, {
      role: 'user', content: 'u-late', clientMessageId: CLIENT_UUID,
    }))
    store.addMessage(PIPELINE_ID, makeMsg('ai-late', 6, { role: 'assistant', content: 'ai-late' }))

    // 向上翻页推回更早消息，其中含一条与本地乐观 user 同 clientMessageId 的 API 版
    store.prependMessages(PIPELINE_ID, [
      makeMsg('ai-old', 1, { role: 'assistant', content: 'ai-old' }),
      makeMsg('record-user-late', 5, { role: 'user', content: 'u-late', clientMessageId: CLIENT_UUID }),
    ], false)

    const msgs = store.getMessages(PIPELINE_ID)
    const uLateMsgs = msgs.filter((m) => m.content === 'u-late')
    // 不应两条并存：乐观 UUID 版被 API 版替换
    expect(uLateMsgs).toHaveLength(1)
    expect(uLateMsgs[0].id).toBe('record-user-late')
  })

  it('场景6: 本地无消息时 append 正常追加全部', () => {
    const store = usePipelineMessageStore.getState()
    store.appendMessages(PIPELINE_ID, [
      makeMsg('user-1', 1, { role: 'user', content: 'u1' }),
      makeMsg('ai-1', 2, { role: 'assistant', content: 'ai1' }),
    ])

    const msgs = store.getMessages(PIPELINE_ID)
    expect(msgs).toHaveLength(2)
    expect(msgs[0].role).toBe('user')
    expect(msgs[1].role).toBe('assistant')
  })
})

/**
 * prependedCountByPipeline 测试：驱动虚拟列表 firstItemIndex，保证 prepend 后视口位置不变。
 * initFromAPI 全量重建时归零；prependMessages 净增 N 条时 +N。
 */
describe('prependedCountByPipeline（虚拟列表 firstItemIndex 驱动）', () => {
  beforeEach(() => {
    usePipelineMessageStore.setState({
      messagesByPipeline: {},
      pipelines: {},
      pipelineSessionMap: {},
      streamingState: {},
      activePipelineId: null,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
      prependedCountByPipeline: {},
      reconciledByPipeline: {},
    })
    const store = usePipelineMessageStore.getState()
    store.registerPipeline({
      pipelineId: PIPELINE_ID,
      sessionId: SESSION_ID,
      level: 1,
      tabId: null,
      agentName: '',
      status: 'idle',
      parentId: null,
      unreadCount: 0,
    } as any)
  })

  it('initFromAPI 后 prependedCount 为 0', () => {
    const store = usePipelineMessageStore.getState()
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('a', 10, { role: 'user', content: 'a' }),
      makeMsg('b', 20, { role: 'assistant', content: 'b' }),
    ])
    expect(store.getPrependedCount(PIPELINE_ID)).toBe(0)
  })

  it('prependMessages 后 prependedCount 累计净增条数', () => {
    const store = usePipelineMessageStore.getState()
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('a', 10, { role: 'user', content: 'a' }),
      makeMsg('b', 20, { role: 'assistant', content: 'b' }),
    ])
    // 向上翻页：插入 2 条更早的历史（sequence 5、8）
    store.prependMessages(PIPELINE_ID, [
      makeMsg('old-1', 5, { role: 'user', content: 'old1' }),
      makeMsg('old-2', 8, { role: 'assistant', content: 'old2' }),
    ])
    expect(store.getPrependedCount(PIPELINE_ID)).toBe(2)
  })

  it('多次 prepend 累加，不被覆盖', () => {
    const store = usePipelineMessageStore.getState()
    store.initFromAPI(PIPELINE_ID, [
      makeMsg('a', 10, { role: 'user', content: 'a' }),
    ])
    store.prependMessages(PIPELINE_ID, [makeMsg('old-1', 5, { role: 'user' })])
    store.prependMessages(PIPELINE_ID, [makeMsg('old-2', 3, { role: 'user' })])
    expect(store.getPrependedCount(PIPELINE_ID)).toBe(2)
  })

  it('initFromAPI 重建后 prependedCount 归零', () => {
    const store = usePipelineMessageStore.getState()
    store.initFromAPI(PIPELINE_ID, [makeMsg('a', 10, { role: 'user', content: 'a' })])
    store.prependMessages(PIPELINE_ID, [makeMsg('old-1', 5, { role: 'user' })])
    expect(store.getPrependedCount(PIPELINE_ID)).toBe(1)

    // 全量重建（刷新/切会话）
    store.initFromAPI(PIPELINE_ID, [makeMsg('a', 10, { role: 'user', content: 'a' })])
    expect(store.getPrependedCount(PIPELINE_ID)).toBe(0)
  })
})
