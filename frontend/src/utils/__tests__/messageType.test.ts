// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * messageType.checkIsSystemMessage 测试
 *
 * 系统消息判定：role 直接命中 / metadata 三字段命中 / 全缺失 false。
 */
import { describe, it, expect } from 'vitest'
import { checkIsSystemMessage } from '@/utils/messageType'

describe('checkIsSystemMessage', () => {
  it('role === system → true（含 metadata 同时存在）', () => {
    expect(checkIsSystemMessage('system', { record_type: 'user' })).toBe(true)
    expect(checkIsSystemMessage('system')).toBe(true)
  })

  it('metadata.record_type / type / sender_type 任一为 system → true', () => {
    expect(checkIsSystemMessage('assistant', { record_type: 'system' })).toBe(true)
    expect(checkIsSystemMessage('assistant', { type: 'system' })).toBe(true)
    expect(checkIsSystemMessage('assistant', { sender_type: 'system' })).toBe(true)
  })

  it('role 非 system 且 metadata 无 system 标记 → false', () => {
    expect(checkIsSystemMessage('user')).toBe(false)
    expect(checkIsSystemMessage('assistant', { record_type: 'message', type: 'text' })).toBe(false)
    expect(checkIsSystemMessage(undefined, { record_type: 'user' })).toBe(false)
    expect(checkIsSystemMessage(undefined, undefined)).toBe(false)
  })
})
