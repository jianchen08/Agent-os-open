// @feature: FP-T12 前端适配 | @ci: frontend-test
/**
 * BUG-72 A1 回归钉：压缩波次后 recordId 对账刷新。
 *
 * 压缩链路原地重写 message_slots 槽位 → 内容寻址指纹（recordId/mc_*）在压缩
 * 后变异。前端跨压缩持有旧指纹时，回退/编辑重发的出站 wire id（recordId ?? id）
 * 指向已不存在的旧槽位。修法 = 压缩波次完成后前端全量对账（initFromAPI），
 * 本文件钉住对账刷新赖以成立的 store 既有契约：
 * 1. 改写槽位（API 版 id=新指纹、metadata 保留 cmid）按 cmid 命中 → 本地旧
 *    指纹版让位 API 权威版 → wire id 刷新为新指纹；
 * 2. 被删除槽位（API 快照无对应）→ 本地残影丢弃（不留下指向空槽位的幽灵）。
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import type * as pipelineMessageStoreMod from '@/stores/pipelineMessageStore'
import type { Message } from '@/types/models'
import { resolveWireUserMessageId } from '@/utils/messageWireId'

vi.mock('@/utils/logger', async () => {
  const { loggerMockFull } = await import('./helpers/storeTestMocks')
  return loggerMockFull()
})

const CMID = 'a1b2c3d4-1111-4a2f-8e8e-1a2b3c4d5e6f'
const OLD_FINGERPRINT = 'mc_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
const NEW_FINGERPRINT = 'mc_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb'

describe('压缩波次后 recordId 对账刷新（BUG-72 A1）', () => {
  let usePipelineMessageStore: pipelineMessageStoreMod.usePipelineMessageStore

  beforeEach(async () => {
    vi.resetModules()
    const { resetPipelineStoreState } = await import('./helpers/storeTestMocks')
    usePipelineMessageStore = await resetPipelineStoreState()
  })

  /** 播种本地已认领 user（UI id=uuid、recordId=压缩前指纹） */
  function seedClaimedUser() {
    usePipelineMessageStore.getState().addMessage('pipe-1', {
      id: 'uuid-local-1',
      sessionId: 'sess-1',
      role: 'user',
      content: '第一问',
      timestamp: new Date().toISOString(),
      status: 'completed',
      clientMessageId: CMID,
      recordId: OLD_FINGERPRINT,
      sequence: 3,
    } as Message)
  }

  it('改写槽位：对账后 wire id 刷新为压缩后新指纹', () => {
    seedClaimedUser()
    // 压缩改写后的 API 权威版：id=新指纹、内容为压缩变体、metadata 保留 cmid
    usePipelineMessageStore.getState().initFromAPI('pipe-1', [
      {
        id: NEW_FINGERPRINT,
        sessionId: 'sess-1',
        role: 'user',
        content: '<compressed>第一问</compressed>',
        timestamp: new Date().toISOString(),
        status: 'completed',
        clientMessageId: CMID,
        sequence: 3,
      } as Message,
    ])

    const msgs = usePipelineMessageStore.getState().getMessages('pipe-1')
    expect(msgs).toHaveLength(1)
    expect(msgs[0]?.id).toBe(NEW_FINGERPRINT)
    // 出站 wire id（回退/编辑重发 user_message_id）指向新指纹——内核槽位可达
    expect(resolveWireUserMessageId(msgs[0] as Message)).toBe(NEW_FINGERPRINT)
    expect(resolveWireUserMessageId(msgs[0] as Message)).not.toBe(OLD_FINGERPRINT)
  })

  it('被删槽位：对账后本地残影丢弃（不留指向空槽位的幽灵）', () => {
    // 5 分钟前认领（飞行中新鲜度窗外）：压缩波次删除后对账即清理
    usePipelineMessageStore.getState().addMessage('pipe-1', {
      id: 'uuid-local-1',
      sessionId: 'sess-1',
      role: 'user',
      content: '第一问',
      timestamp: new Date(Date.now() - 300_000).toISOString(),
      status: 'completed',
      clientMessageId: CMID,
      recordId: OLD_FINGERPRINT,
      sequence: 3,
    } as Message)
    usePipelineMessageStore.getState().addMessage('pipe-1', {
      id: 'mc_assistant_old',
      sessionId: 'sess-1',
      role: 'assistant',
      content: '旧回复',
      timestamp: new Date().toISOString(),
      status: 'completed',
      sequence: 4,
    } as Message)
    // 压缩波次删除两条槽位后的 API 快照：只剩更早的另一条 user
    usePipelineMessageStore.getState().initFromAPI('pipe-1', [
      {
        id: 'mc_earlier_user',
        sessionId: 'sess-1',
        role: 'user',
        content: '更早的问题',
        timestamp: new Date(Date.now() - 60_000).toISOString(),
        status: 'completed',
        sequence: 1,
      } as Message,
    ])

    const msgs = usePipelineMessageStore.getState().getMessages('pipe-1')
    expect(msgs.map((m) => m.id)).toEqual(['mc_earlier_user'])
  })

  it('飞行中保护不误伤：压缩对账保留窗口内未认领乐观 user（cmid 无 API 对应）', () => {
    usePipelineMessageStore.getState().addMessage('pipe-1', {
      id: 'uuid-inflight',
      sessionId: 'sess-1',
      role: 'user',
      content: '压缩发生瞬间刚发出的消息',
      timestamp: new Date().toISOString(),
      status: 'sending',
      clientMessageId: 'cmid-inflight-fresh',
      sequence: 99,
    } as Message)
    // API 快照只含压缩产物，不含在途消息
    usePipelineMessageStore.getState().initFromAPI('pipe-1', [
      {
        id: 'mc_block',
        sessionId: 'sess-1',
        role: 'system',
        content: '<compressed seq="0-2" level="L1">\n摘要\n</compressed>',
        timestamp: new Date().toISOString(),
        status: 'completed',
        sequence: 0,
      } as Message,
    ])

    const msgs = usePipelineMessageStore.getState().getMessages('pipe-1')
    expect(msgs.some((m) => m.clientMessageId === 'cmid-inflight-fresh')).toBe(true)
  })
})
