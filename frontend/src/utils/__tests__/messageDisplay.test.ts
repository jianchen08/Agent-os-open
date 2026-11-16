/** @feature FP-0.2.四 渲染用消息流水线（buildRenderableMessages 直测） | @ci: frontend-test */
/**
 * 已吸收 tool 消息过滤契约：toolCallId 已入前序 assistant 的 tool_call part
 * → 渲染层跳过（避免与 ActivityCard 重复）；无 toolCallId / 未被吸收 → 保留
 * 兜底（结果不丢失）。仅过滤渲染层，不改原始数据。
 */
import { describe, expect, it, vi } from 'vitest'

vi.mock('@/services/api/session', () => ({
  // 合并面另有专测，此处恒等注入以聚焦 tool 过滤层
  mergeConsecutiveAssistantMessages: (xs: unknown[]) => xs,
}))

import { buildRenderableMessages } from '@/utils/messageDisplay'
import type { Message } from '@/types/models'

function msg(role: Message['role'], over: Partial<Message> = {}): Message {
  return { id: 'm', sessionId: 's', sequence: 1, role, content: '', timestamp: '2026-10-20T00:00:00Z', status: 'done', ...over } as Message
}

describe('buildRenderableMessages 已吸收 tool 过滤', () => {
  it('toolCallId 被吸收 → 跳过；未吸收/无 id → 保留兜底', () => {
    const input = [
      msg('assistant', {
        id: 'a1',
        parts: [{ type: 'tool_call', callId: 'c1', name: 'search', args: {}, state: 'completed' }],
      }),
      msg('tool', { id: 't1', toolCallId: 'c1' }),
      msg('tool', { id: 't2', toolCallId: 'c2' }),
      msg('tool', { id: 't3' }),
      msg('user', { id: 'u1', content: 'hi' }),
    ]
    const out = buildRenderableMessages(input)
    expect(out.map((m) => m.id)).toEqual(['a1', 't2', 't3', 'u1'])
  })
})
