/**
 * 认领模块单测（ADR 2026-08-22「认领替代驱逐」核心语义）。
 *
 * 回归锚点：用户真机症状①③「发送后用户消息消失」——认领升级必须保留 UI id、
 * 权威 id 记入独立 recordId 字段、幂等重入不重复升级、候选缺失时补插权威版。
 */
import { describe, it, expect } from 'vitest'
import { decideClaim, type ClaimCandidate, type UserRecord } from '../claim'

function userRecord(over: Partial<UserRecord> = {}): UserRecord {
  return { id: 'mc_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', ...over }
}

function candidate(over: Partial<ClaimCandidate> = {}): ClaimCandidate {
  return { id: '9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f', clientMessageId: '9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f', status: 'sending', ...over }
}

describe('decideClaim 认领裁决', () => {
  it('乐观候选命中 → upgrade：UI id 保持不变，权威 id 记入 recordId + 补 seq', () => {
    const act = decideClaim(candidate(), userRecord({ sequence: 42 }), '9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f')
    expect(act).toEqual({
      kind: 'upgrade',
      messageId: '9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f',
      recordId: 'mc_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
      sequence: 42,
    })
    // UI 寻址 id 永不迁移（2b1940b00 双字段范式）——messageId 必须仍是前端 uuid
    expect(act.kind).toBe('upgrade')
    if (act.kind === 'upgrade') expect(act.messageId).toBe('9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f')
  })

  it('同 recordId 重入 → skip（幂等：重复 new_message / 对账重入不重复升级）', () => {
    const already = candidate({ recordId: 'mc_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', status: 'completed' })
    const act = decideClaim(already, userRecord(), '9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f')
    expect(act).toEqual({ kind: 'skip', reason: expect.stringContaining('幂等') })
  })

  it('候选缺失（pending 已撤下/刷新后确认到达）→ insert：权威 user 不能丢', () => {
    const act = decideClaim(undefined, userRecord({ sequence: 7 }), '9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f')
    expect(act).toEqual({
      kind: 'insert',
      messageId: '9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f',
      recordId: 'mc_0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef',
      sequence: 7,
    })
  })

  it('cmid 不匹配 → skip（不认领别人的消息）', () => {
    const other = candidate({ clientMessageId: 'other-cmid' })
    const act = decideClaim(other, userRecord(), '9c8e051a-4a2f-4e8e-b2b1-1a2b3c4d5e6f')
    expect(act).toEqual({ kind: 'skip', reason: expect.stringContaining('cmid 不匹配') })
  })

  it('user_record 缺权威 id（旧后端未回传）→ skip 不升级不插入', () => {
    const act = decideClaim(candidate(), userRecord({ id: undefined }), 'cmid')
    expect(act).toEqual({ kind: 'skip', reason: expect.stringContaining('缺权威 id') })
  })
})
