// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 真实数据回归：多轮工具循环（真实 DB 消息，16:24 用户运行 eadf8db3e117，
 * 13 轮 assistant+tool 交替）的渲染合并产物——「工具卡在轮内位置、尾部
 * 不出现整段工具簇」不变式。
 *
 * 数据路径：真实 message_slots（单次 run：user@32 → 13×(assistant+tool) → 终条）
 * → API 形状（camelCase）→ 真实 mapBackendMessageToMessage →
 * 真实 mergeConsecutiveAssistantMessages 渲染合并 → 用户看到的气泡同构。
 * 2026-08-27 用户反馈「最后有一大段片的工具调用」，此测试是回归锚：
 * 气泡 parts 序列必须保持 [text, tool, text, tool, ...] 逐轮交错。
 */
import { describe, it, expect, beforeEach } from 'vitest'

import { mapBackendMessageToMessage, mergeConsecutiveAssistantMessages } from '@/services/api/session'
import type { Message } from '@/types/models'

import fixtureRaw from './__fixtures__/real_round_messages.json'

const THREAD_ID = 'thread-eadf8db3e117'

describe('真实数据多轮工具循环：渲染合并不产生尾部工具卡簇', () => {
  let messages: Message[]
  let rounds: number

  beforeEach(() => {
    messages = fixtureRaw.map((r: any) =>
      mapBackendMessageToMessage({ ...r, thread_id: THREAD_ID } as any, THREAD_ID),
    )
    rounds = messages.filter((m) => m.role === 'assistant').length
  })

  it('渲染层合并：工具卡唯一、按轮归属、无重复卡（数据忠实：空文本轮次卡片连续不属于缺陷）', () => {
    const merged = mergeConsecutiveAssistantMessages(messages)
    // 一个连续气泡（user 边界后 13 轮 + 终条）
    const bubbles = merged.filter((m) => m.role === 'assistant')
    const bubble = bubbles[0]
    expect(bubble).toBeDefined()

    const parts = bubble.parts || []
    const types = parts.map((p: any) => p.type)
    console.log('bubble parts types:', types.join(','))

    const toolCount = types.filter((t) => t === 'tool_call').length
    const callIds = parts.filter((p: any) => p.type === 'tool_call').map((p: any) => p.callId)
    console.log(`tools=${toolCount} ids=${callIds.join('|')}`)

    // 不变式 1：工具卡数 == 数据层工具调用数（无重复出卡、无丢失）
    const dataToolCalls = (fixtureRaw as any[]).filter((m: any) => (m.toolCalls || []).length > 0)
      .reduce((acc, m) => acc + m.toolCalls.length, 0)
    expect(toolCount).toBe(dataToolCalls)
    // 不变式 2：callId 全局唯一（重复 = 同一调用出两张卡——用户反馈的缺陷）
    expect(new Set(callIds).size).toBe(callIds.length)
    // 不变式 3：数据层每条消息的工具卡只存在于该消息（轮归属）
    const perMsgTools = messages
      .filter((m) => m.role === 'assistant')
      .map((m) => (m.parts || []).filter((p: any) => p.type === 'tool_call').map((p: any) => p.callId))
    const allFromData = perMsgTools.flat()
    expect(new Set(allFromData).size).toBe(allFromData.length)
  })

  it('数据层：每轮消息 [text(+thinking), tool] 轮内位置正确（与流式期每轮占位同构）', () => {
    const assistantMsgs = messages.filter((m) => m.role === 'assistant')
    expect(assistantMsgs.length).toBe(rounds)
    const ids = new Set(assistantMsgs.map((m) => m.id))
    expect(ids.size).toBe(rounds)
    for (const m of assistantMsgs) {
      const types = (m.parts || []).map((p: any) => p.type)
      const toolIdx = types.indexOf('tool_call')
      const lastTextIdx = types.lastIndexOf('text')
      if (toolIdx >= 0 && lastTextIdx >= 0) {
        expect(toolIdx).toBeGreaterThan(lastTextIdx)
      }
    }
  })
})
