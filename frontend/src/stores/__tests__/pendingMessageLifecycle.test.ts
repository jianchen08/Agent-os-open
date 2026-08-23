/**
 * @feature 消息幂等契约 | @ci frontend-test
 *
 * 乐观 user 生命周期（ADR 2026-08-22 单一消息数组 + 认领替代驱逐）：
 * 发送瞬间直接进主数组（status='sending'），确认源三路全部升级/收敛而非驱逐——
 * ① new_message 事件（user_message 权威回传 → 认领 upgrade：UI id 不变、
 *    recordId 记入独立字段）
 * ② initFromAPI 全量对账（API user 消息 metadata.client_message_id / record_id
 *    双键收敛，本地乐观版让位 API 权威版，不并存不重复）
 * ③ appendMessages 增量补漏（重连 backfill，同键收敛）
 * 主数组即唯一消息面（无独立 pending 区）——「发送后用户消息消失」结构性不可能。
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'

import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { handleNewMessage } from '@/services/websocket/streaming/handlers'

const PIPELINE_ID = 'pipe-pending-life'
const THREAD_ID = 'thread-pending-life'

describe('乐观 user 生命周期（ADR 2026-08-22 单一消息数组）', () => {
  beforeEach(() => {
    usePipelineMessageStore.setState({
      messagesByPipeline: { [PIPELINE_ID]: [] },
      pipelines: {},
      pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID },
      streamingState: {},
      activePipelineId: PIPELINE_ID,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
      reconciledByPipeline: {},
    })
  })

  const mkOptimistic = (cmid: string, content = '在途消息') => ({
    id: cmid,
    sessionId: THREAD_ID,
    role: 'user' as const,
    content,
    timestamp: new Date().toISOString(),
    status: 'sending' as const,
    clientMessageId: cmid,
  })

  const mkApiUser = (id: string, seq: number, cmid?: string) => ({
    id,
    sessionId: THREAD_ID,
    role: 'user' as const,
    content: '在途消息',
    timestamp: new Date().toISOString(),
    sequence: seq,
    status: 'completed' as const,
    ...(cmid ? { clientMessageId: cmid } : {}),
  })

  it('发送瞬间进主数组：同一数组承载乐观 user 与已确认消息（无独立 pending 区）', () => {
    const ps = usePipelineMessageStore.getState()
    ps.addMessage(PIPELINE_ID, mkOptimistic('cmid-1'))
    ps.addMessage(PIPELINE_ID, mkOptimistic('cmid-1')) // 同 id 重复 = 原地合并

    const st = usePipelineMessageStore.getState()
    const msgs = st.getMessages(PIPELINE_ID)
    expect(msgs.filter((m) => m.clientMessageId === 'cmid-1')).toHaveLength(1)
    expect(msgs[0].status).toBe('sending')
  })

  it('确认源①：new_message 携带 user_message 权威回传 → 认领升级（UI id 不变 + recordId 独立字段）', () => {
    const ps = usePipelineMessageStore.getState()
    ps.addMessage(PIPELINE_ID, mkOptimistic('cmid-a'))
    ps.addMessage(PIPELINE_ID, mkOptimistic('cmid-b'))

    // 先建流式占位（new_message 只更新已存在消息，不新建；与本断言无关，
    // 但保持事件链真实：stream_start 建占位 → new_message 收尾）
    handleNewMessage({
      type: 'new_message',
      pipeline_id: PIPELINE_ID,
      message_id: 'a_msg1',
      _threadId: THREAD_ID,
      data: {
        pipeline_id: PIPELINE_ID,
        message_id: 'a_msg1',
        client_message_id: 'cmid-a',
        user_message: { id: 'mc_aaaaaaaa', content: '在途消息', sequence: 3 },
      },
    })

    const st = usePipelineMessageStore.getState()
    const msgs = st.getMessages(PIPELINE_ID)
    const claimed = msgs.find((m) => m.clientMessageId === 'cmid-a')
    expect(claimed).toBeDefined()
    expect(claimed?.id).toBe('cmid-a') // UI 寻址 id 永不迁移
    expect(claimed?.recordId).toBe('mc_aaaaaaaa') // 权威 id 记入独立字段
    expect(claimed?.status).toBe('completed')
    expect(claimed?.sequence).toBe(3)
    // 另一条乐观消息未确认 → 仍 sending（不误伤）
    expect(msgs.find((m) => m.clientMessageId === 'cmid-b')?.status).toBe('sending')
  })

  it('确认源②：initFromAPI 返回同 cmid 的 user 权威版 → 本地乐观版让位 API 版，不并存', () => {
    const ps = usePipelineMessageStore.getState()
    ps.initFromAPI(PIPELINE_ID, [
      { id: 'api-1', sessionId: THREAD_ID, role: 'user', content: '历史', timestamp: new Date().toISOString(), sequence: 1, status: 'completed' },
    ])
    ps.addMessage(PIPELINE_ID, mkOptimistic('cmid-x'))

    // 刷新对账：API 返回 user 权威版（id=后端 record，metadata.client_message_id=cmid-x）
    ps.initFromAPI(PIPELINE_ID, [
      { id: 'api-1', sessionId: THREAD_ID, role: 'user', content: '历史', timestamp: new Date().toISOString(), sequence: 1, status: 'completed' },
      mkApiUser('rec-9f2a', 2, 'cmid-x'),
    ])

    const st = usePipelineMessageStore.getState()
    // 主 store 恰好一条该内容的 user 消息（API 权威版）——不并存、不重复
    const users = st.getMessages(PIPELINE_ID).filter((m) => m.role === 'user')
    expect(users.length).toBe(2)
    expect(users.some((m) => m.id === 'cmid-x')).toBe(false) // 乐观 UUID 版让位
    expect(users.some((m) => m.id === 'rec-9f2a')).toBe(true)
    expect(users.find((m) => m.id === 'rec-9f2a')?.clientMessageId).toBe('cmid-x')
  })

  it('确认源③：appendMessages（重连 backfill）返回同 cmid → 乐观版让位 API 权威版', () => {
    const ps = usePipelineMessageStore.getState()
    ps.initFromAPI(PIPELINE_ID, [])
    ps.addMessage(PIPELINE_ID, mkOptimistic('cmid-y'))

    ps.appendMessages(PIPELINE_ID, [mkApiUser('rec-y111', 1, 'cmid-y')])

    const st = usePipelineMessageStore.getState()
    const msgs = st.getMessages(PIPELINE_ID)
    expect(msgs.find((m) => m.id === 'rec-y111')).toBeDefined()
    expect(msgs.some((m) => m.id === 'cmid-y')).toBe(false) // 不并存
  })

  it('未确认的乐观消息不被无关对账收敛（不同 cmid / 无 cmid 消息）', () => {
    const ps = usePipelineMessageStore.getState()
    ps.initFromAPI(PIPELINE_ID, [])
    ps.addMessage(PIPELINE_ID, mkOptimistic('cmid-z'))

    ps.appendMessages(PIPELINE_ID, [
      mkApiUser('rec-other', 1, 'cmid-unrelated'),
      { id: 'rec-nocmid', sessionId: THREAD_ID, role: 'user', content: '无键', timestamp: new Date().toISOString(), sequence: 2, status: 'completed' },
    ])

    const st = usePipelineMessageStore.getState()
    // 未确认乐观消息保留（等确认或超时）
    expect(st.getMessages(PIPELINE_ID).find((m) => m.id === 'cmid-z')?.status).toBe('sending')
  })

  it('ensureStreamingPlaceholder 精确 ID 生命周期：同 id 已存在 → 原地保留不新建不抹内容', async () => {
    vi.resetModules()
    const storeMod = await import('@/stores/pipelineMessageStore')
    const store = storeMod.usePipelineMessageStore
    store.setState({
      messagesByPipeline: { [PIPELINE_ID]: [] },
      pipelines: {},
      pipelineSessionMap: { [PIPELINE_ID]: THREAD_ID },
      streamingState: {},
      activePipelineId: PIPELINE_ID,
      topCursorsByPipeline: {},
      bottomCursorsByPipeline: {},
      hasMoreOlderByPipeline: {},
      isLoadingOlderByPipeline: {},
      reconciledByPipeline: {},
    })

    const { ensureStreamingPlaceholder } = await import(
      '@/services/websocket/streaming/handlers/utils'
    )

    // chunk 先于 start 自动建占位（真实后端 message_id）并积累内容
    ensureStreamingPlaceholder(PIPELINE_ID, 'a_real_1', THREAD_ID)
    store.getState().updateMessage(PIPELINE_ID, 'a_real_1', { content: '已积累的流式内容' })

    // stream_start 同 id 再达：原地保留，内容不得被空占位抹掉
    ensureStreamingPlaceholder(PIPELINE_ID, 'a_real_1', THREAD_ID)

    const msgs = store.getState().getMessages(PIPELINE_ID)
    expect(msgs.filter((m) => m.id === 'a_real_1').length).toBe(1)
    expect(msgs.find((m) => m.id === 'a_real_1')?.content).toBe('已积累的流式内容')
    // 占位无本地拼 seq（等 stream_end final_sequence 权威值）
    expect(msgs.find((m) => m.id === 'a_real_1')?.sequence).toBeUndefined()
  })
})
