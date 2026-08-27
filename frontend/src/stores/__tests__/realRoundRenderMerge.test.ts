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

  it('渲染层合并：工具卡与文本逐轮交错，无尾部整段簇', () => {
    const merged = mergeConsecutiveAssistantMessages(messages)
    // 一个连续气泡（user 边界后 13 轮 + 终条）
    const bubbles = merged.filter((m) => m.role === 'assistant')
    const bubble = bubbles[0]
    expect(bubble).toBeDefined()

    const parts = bubble.parts || []
    const types = parts.map((p: any) => p.type)
    console.log('bubble parts types:', types.join(','))

    const textCount = types.filter((t) => t === 'text').length
    const toolCount = types.filter((t) => t === 'tool_call').length
    console.log(`text=${textCount} tool=${toolCount}`)

    // 不变式 1：全部工具卡都在、callId 唯一（无重复出卡）
    const callIds = parts.filter((p: any) => p.type === 'tool_call').map((p: any) => p.callId)
    expect(toolCount).toBe(rounds - 1) // 最后一轮为纯文本
    expect(new Set(callIds).size).toBe(toolCount)

    // 不变式 2：尾部无整段工具簇——从后往前，最后一个 text 之后不允许出现
    // 「连续 ≥2 张工具卡」；逐轮交替的弱形式：任意工具卡 i 满足——
    // 若其后还有文本，则其与下一个文本之间不允许出现第 2 张工具卡。
    let cluster = false
    let toolRun = 0
    for (let i = parts.length - 1; i >= 0; i--) {
      if (parts[i].type === 'text') {
        toolRun = 0
      } else if (parts[i].type === 'tool_call') {
        toolRun += 1
        if (toolRun >= 2) cluster = true
      }
    }
    expect(cluster).toBe(false)

    // 不变式 3：每张工具卡的「前序文本数」≥「前序工具卡数」（工具卡不先于
    // 其所属轮次的文本出现——即不存在「文本前先堆工具卡」）
    let textSeen = 0
    let toolSeen = 0
    for (const p of parts) {
      if (p.type === 'text') textSeen += 1
      if (p.type === 'tool_call') {
        toolSeen += 1
        expect(toolSeen).toBeLessThanOrEqual(textSeen + 1) // 工具卡 ≤ 前序文本+1（首轮可能无文本）
      }
    }
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
