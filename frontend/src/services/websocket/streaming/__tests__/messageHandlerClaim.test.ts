/** @feature FP-T12 前端适配 | @ci frontend-test */
/**
 * new_message 认领回归测试（ADR 2026-08-22「认领替代驱逐」）。
 *
 * 回归锚 = 用户真机症状①③：发送后用户消息消失（pending 驱逐后无人补权威版）、
 * 刷新后恢复。本测试锁定「new_message 携带 user_message → 认领升级 → UI 气泡
 * 不消失、id 不迁移」。
 */
import { describe, it, expect, beforeEach } from 'vitest'
import { handleNewMessage } from '../handlers/messageHandler'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { usePipelineRegistryStore } from '@/stores/pipelineRegistryStore'

const PIPELINE = 'c1b2c3d4e5f64789abcdef0123456789'
const THREAD = 'thread-1'
const CMID = '9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f'
const RECORD_ID = 'mc_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'

function resetStores(): void {
  usePipelineMessageStore.setState({
    messagesByPipeline: {},
    streamingState: {},
    pipelines: {},
    pipelineSessionMap: {},
    activePipelineId: null,
  } as never)
  useAgentTabStore.setState({ tabs: [] } as never)
  usePipelineRegistryStore.setState({} as never)
}

describe('handleNewMessage 认领', () => {
  beforeEach(resetStores)

  it('user_message 权威回传 → 乐观 user 升级为权威（UI id 不变 + recordId 记入独立字段）', () => {
    const store = usePipelineMessageStore.getState()
    // 发送瞬间：乐观 user 直接进主数组（router.tsx 发送路径同款，单一消息数组）
    store.addMessage(PIPELINE, {
      id: CMID,
      sessionId: THREAD,
      role: 'user',
      content: '你好',
      timestamp: new Date().toISOString(),
      status: 'sending',
      clientMessageId: CMID,
    } as never)
    // assistant 流式占位（stream_start 已建）
    store.addMessage(PIPELINE, {
      id: 'a_0123456789abcdef0123456789abcdef',
      sessionId: THREAD,
      role: 'assistant',
      content: '部分回复',
      timestamp: new Date().toISOString(),
      status: 'streaming',
    } as never)

    handleNewMessage({
      data: {
        pipeline_id: PIPELINE,
        message_id: 'a_0123456789abcdef0123456789abcdef',
        _threadId: THREAD,
        client_message_id: CMID,
        sequence: 5,
        content: '完整回复',
        user_message: { id: RECORD_ID, content: '你好', sequence: 4, metadata: { client_message_id: CMID } },
        message: {
          id: 'a_0123456789abcdef0123456789abcdef',
          role: 'assistant',
          content: '完整回复',
          sequence: 5,
          timestamp: new Date().toISOString(),
          status: 'completed',
        },
      },
    } as never)

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE)
    // 症状①回归锚：user 消息必须还在（认领升级而非驱逐）
    const user = msgs.find((m) => m.clientMessageId === CMID)
    expect(user).toBeDefined()
    expect(user?.id).toBe(CMID) // UI 寻址 id 永不迁移（React key 稳定）
    expect(user?.recordId).toBe(RECORD_ID) // 权威 id 记入独立字段
    expect(user?.status).toBe('completed')
    expect(user?.sequence).toBe(4) // 权威 seq 补正（排序键）
    // 单一消息数组：主数组即唯一消息面（无 pending 区）
    expect(msgs.filter((m) => m.role === 'user')).toHaveLength(1)
    // assistant 收尾
    const assistant = msgs.find((m) => m.id === 'a_0123456789abcdef0123456789abcdef')
    expect(assistant?.status).toBe('completed')
  })

  it('候选缺失（pending 已撤下/断线期间确认到达）→ 补插权威 user（不丢消息）', () => {
    const store = usePipelineMessageStore.getState()
    // 无乐观候选（如刷新后对账窗口）
    handleNewMessage({
      data: {
        pipeline_id: PIPELINE,
        message_id: 'a_11111111111111111111111111111111',
        _threadId: THREAD,
        client_message_id: CMID,
        sequence: 3,
        content: 'ok',
        user_message: { id: RECORD_ID, content: '你好', sequence: 2, metadata: { client_message_id: CMID } },
        message: { id: 'a_11111111111111111111111111111111', role: 'assistant', content: 'ok', sequence: 3, status: 'completed' },
      },
    } as never)
    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE)
    const user = msgs.find((m) => m.recordId === RECORD_ID)
    expect(user).toBeDefined()
    expect(user?.id).toBe(CMID)
    expect(user?.status).toBe('completed')
    expect(user?.sequence).toBe(2)
  })

  it('旧内核无 user_message → 乐观 user 标记 completed（不驱逐不删除，兼容不崩）', () => {
    const store = usePipelineMessageStore.getState()
    store.addMessage(PIPELINE, {
      id: CMID, sessionId: THREAD, role: 'user', content: 'hi',
      timestamp: new Date().toISOString(), status: 'sending', clientMessageId: CMID,
    } as never)
    handleNewMessage({
      data: {
        pipeline_id: PIPELINE,
        message_id: 'a_22222222222222222222222222222222',
        _threadId: THREAD,
        client_message_id: CMID,
        sequence: 1,
        content: 'ok',
        message: { id: 'a_22222222222222222222222222222222', role: 'assistant', content: 'ok', sequence: 1, status: 'completed' },
      },
    } as never)
    // 无 user_message → 不插权威版（旧内核无回传），乐观 user 原地 completed
    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE)
    const user = msgs.find((m) => m.clientMessageId === CMID)
    expect(user).toBeDefined()
    expect(user?.status).toBe('completed')
  })

  it('服务端 status=interrupted（停止/中断半截消息）→ 前端保持 interrupted 不覆盖 completed', () => {
    const store = usePipelineMessageStore.getState()
    // 流式占位（stream_start 已建，半截内容已流出）
    store.addMessage(PIPELINE, {
      id: 'a_33333333333333333333333333333333',
      sessionId: THREAD,
      role: 'assistant',
      content: '半截',
      timestamp: new Date().toISOString(),
      status: 'streaming',
    } as never)

    handleNewMessage({
      data: {
        pipeline_id: PIPELINE,
        message_id: 'a_33333333333333333333333333333333',
        _threadId: THREAD,
        sequence: 7,
        content: '半截',
        message: {
          id: 'a_33333333333333333333333333333333',
          role: 'assistant',
          content: '半截',
          sequence: 7,
          timestamp: new Date().toISOString(),
          status: 'interrupted',
        },
      },
    } as never)

    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE)
    const assistant = msgs.find((m) => m.id === 'a_33333333333333333333333333333333')
    expect(assistant?.status).toBe('interrupted')
    // 区分度：服务端 error 同样透传（非一律 completed）
    expect(assistant?.status).not.toBe('completed')
  })
})

/** 无 cmid 注入消息（触发器/任务/HTTP，ADR-2026-08-26）补插 user 气泡 */
describe('handleNewMessage 注入消息补插', () => {
  it('无 cmid 有 user_message → 补插 user 气泡（按 id 幂等 + 按 seq 落位）', () => {
    const store = usePipelineMessageStore.getState()
    // 前置：assistant 回复已在主数组（seq=2）
    store.addMessage(PIPELINE, {
      id: 'a_11111111111111111111111111111111',
      sessionId: THREAD,
      role: 'assistant',
      content: '收到，开始执行。',
      timestamp: new Date().toISOString(),
      status: 'completed',
      sequence: 2,
    } as never)

    // 触发器注入的 user 消息（无 cmid；user_message 权威回传）
    const userRecord = {
      id: 'mc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      content: '触发器：每天 9 点提醒喝水',
      sequence: 1,
      metadata: { source: 'trigger' },
    }
    handleNewMessage({
      type: 'new_message',
      data: {
        pipeline_id: PIPELINE,
        _threadId: THREAD,
        message_id: 'a_11111111111111111111111111111111',
        sequence: 2,
        user_message: userRecord,
        message: {
          id: 'a_11111111111111111111111111111111',
          role: 'assistant',
          content: '收到，已设提醒',
          sequence: 2,
          timestamp: new Date().toISOString(),
          status: 'completed',
        },
      },
    })
    const msgs = usePipelineMessageStore.getState().getMessages(PIPELINE)
    const injected = msgs.find((m) => m.recordId === userRecord.id)
    expect(injected).toBeDefined()
    expect(injected?.content).toBe('触发器：每天 9 点提醒喝水')
    // 位置：user(seq=1) 在 assistant(seq=2) 之前
    const userIdx = msgs.findIndex((m) => m.recordId === userRecord.id)
    const asstIdx = msgs.findIndex((m) => m.id === 'a_11111111111111111111111111111111')
    expect(userIdx).toBeGreaterThanOrEqual(0)
    expect(userIdx).toBeLessThan(asstIdx)

    // 重复事件幂等：再次派发同 user_message → 不双插
    handleNewMessage({
      type: 'new_message',
      data: {
        pipeline_id: PIPELINE,
        _threadId: THREAD,
        sequence: 2,
        user_message: userRecord,
        message: {
          id: 'a_11111111111111111111111111111111',
          role: 'assistant',
          content: '已完成',
          sequence: 2,
          timestamp: new Date().toISOString(),
          status: 'completed',
        },
      },
    })
    const after = usePipelineMessageStore.getState().getMessages(PIPELINE)
    expect(after.filter((m) => m.recordId === userRecord.id)).toHaveLength(1)
  })
})
