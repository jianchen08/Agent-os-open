/** @feature FP-T12 None | @ci: frontend-test */
/** @feature 工具卡中断终态（历史孤儿工具调用显式终态） | @ci: frontend-test */
/**
 * useMessageRender / buildFragmentsFromParts 孤儿工具调用终态渲染回归。
 *
 * 三组区分输入（验收判据）：
 * (a) 无结果 + 非流式上下文 → interrupted 静止终态（statusText 标注、无时长）；
 * (b) 有结果 → completed 现状不变；
 * (c) 流式上下文无结果 → running 现状不变（真在途不误判）。
 *
 * 另覆盖：
 * - 流式 → 停止的翻转（live 孤儿：结果事件永不到达，流结束后卡转终态）；
 * - 真实冷路径集成：mapBackendMessageToMessage + mergeConsecutiveAssistantMessages
 *   的历史孤儿（无 tool 结果消息）→ interrupted；
 * - 流式上下文内的跨轮幽灵卡（退化/重试轮遗留的声明卡，结果永不到达）：
 *   其后再无模型推送才可算在途，已有新一轮推送 → interrupted（不再永动转圈）。
 */
import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  buildFragmentsFromParts,
  useMessageRender,
} from '@/components/chat/hooks/useMessageRender'
import {
  mapBackendMessageToMessage,
  mergeConsecutiveAssistantMessages,
  type BackendMessageResponse,
} from '@/services/api/session'
import { registerGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import type { ToolCallPart } from '@/types/messageParts'
import type { Message, MessagePart } from '@/types/models'

const BASE_MSG: Message = {
  id: 'msg-orphan-001',
  sessionId: 'sess-orphan',
  role: 'assistant',
  content: '',
  timestamp: '2026-10-23T20:41:00.000Z',
  parentId: null,
  status: 'completed',
}

function makeMessage(parts: MessagePart[]): Message {
  return { ...BASE_MSG, parts }
}

/** 历史孤儿 tool_call part（冷路径形态：无结果无错误，state 停在在途） */
function orphanPart(overrides?: Partial<ToolCallPart>): ToolCallPart {
  return {
    type: 'tool_call',
    callId: 'call_1c7baf51',
    name: 'human_interaction',
    args: { message: '请拍板' },
    state: 'streaming',
    sequence: 1,
    ...overrides,
  }
}

function expectToolFragment(fragments: ReturnType<typeof buildFragmentsFromParts>) {
  const frag = fragments.find((f) => f.type === 'tool_call')
  if (!frag || frag.type !== 'tool_call') throw new Error('expected tool_call fragment')
  return frag
}

beforeEach(() => {
  registerGlobalOpenFileCallback(() => {})
})

describe('buildFragmentsFromParts 孤儿工具调用终态', () => {
  it('(a) 无结果 + 非流式上下文 → interrupted 终态，带标注文本、不伪造时长', () => {
    const fragments = buildFragmentsFromParts(makeMessage([orphanPart()]), undefined, {
      streaming: false,
    })
    expect(fragments).toHaveLength(1)
    const frag = expectToolFragment(fragments)
    expect(frag.activity.status).toBe('interrupted')
    expect(frag.activity.statusText).toBe('已中断（无结果）')
    expect(frag.activity.durationMs).toBeUndefined()
  })

  it('(b) 有结果 → completed 现状不变，无中断标注', () => {
    const fragments = buildFragmentsFromParts(
      makeMessage([orphanPart({ state: 'done', result: '用户选了 A', durationMs: 1200 })]),
      undefined,
      { streaming: false },
    )
    const frag = expectToolFragment(fragments)
    expect(frag.activity.status).toBe('completed')
    expect(frag.activity.statusText).toBeUndefined()
    expect(frag.activity.durationMs).toBe(1200)
  })

  it('(b2) 错误证据 → failed 现状不变（非流式上下文不吞错误终态）', () => {
    const fragments = buildFragmentsFromParts(
      makeMessage([orphanPart({ state: 'error', error: 'boom' })]),
      undefined,
      { streaming: false },
    )
    const frag = expectToolFragment(fragments)
    expect(frag.activity.status).toBe('failed')
    expect(frag.activity.statusText).toBeUndefined()
  })

  it('(c) 流式上下文无结果 → running 现状不变（calling 与 streaming 在途同义）', () => {
    for (const state of ['calling', 'streaming'] as const) {
      const fragments = buildFragmentsFromParts(makeMessage([orphanPart({ state })]), undefined, {
        streaming: true,
      })
      const frag = expectToolFragment(fragments)
      expect(frag.activity.status).toBe('running')
      expect(frag.activity.statusText).toBeUndefined()
    }
  })

  it('流式翻转停止（结果永不到达）→ running 转 interrupted（live 孤儿收口）', () => {
    const { result, rerender } = renderHook(
      ({ generating }: { generating: boolean }) =>
        useMessageRender({
          message: makeMessage([orphanPart({ state: 'calling' })]),
          isGenerating: generating,
          isLast: true,
        }),
      { initialProps: { generating: true } },
    )
    expect(expectToolFragment(result.current.fragments).activity.status).toBe('running')

    rerender({ generating: false })
    const frag = expectToolFragment(result.current.fragments)
    expect(frag.activity.status).toBe('interrupted')
    expect(frag.activity.statusText).toBe('已中断（无结果）')
  })

  it('历史孤儿（冷路径真实形态：tool_calls 落库但结果消息缺失）→ interrupted', () => {
    // 后端持久化形态：assistant.tool_calls（OpenAI 形态，arguments 为 JSON 字符串），
    // 对应 role=tool 结果消息从未落库（run 被中断）。
    const backend: BackendMessageResponse = {
      id: 'a_fb01577f546e',
      thread_id: 'sess-orphan',
      sequence: 440,
      role: 'assistant',
      content: '',
      timestamp: '2026-10-23T20:41:00.000Z',
      status: 'interrupted',
      toolCalls: [
        {
          callId: 'call_1c7baf51',
          toolName: 'human_interaction',
          function: { name: 'human_interaction', arguments: '{"message":"请拍板"}' },
        },
      ],
    }
    const loaded = mapBackendMessageToMessage(backend, 'sess-orphan')
    const merged = mergeConsecutiveAssistantMessages([loaded])
    expect(merged).toHaveLength(1)
    const { result } = renderHook(() =>
      useMessageRender({ message: merged[0], isGenerating: false, isLast: false }),
    )
    const frag = expectToolFragment(result.current.fragments)
    expect(frag.activity.status).toBe('interrupted')
    expect(frag.activity.statusText).toBe('已中断（无结果）')
  })
})

describe('useMessageRender isStreaming 判定上下文（工具卡终态判定输入）', () => {
  it('非最后一条消息即便 isGenerating 也不算流式上下文', () => {
    const { result } = renderHook(() =>
      useMessageRender({
        message: makeMessage([orphanPart({ state: 'calling' })]),
        isGenerating: true,
        isLast: false,
      }),
    )
    expect(result.current.isStreaming).toBe(false)
    expect(expectToolFragment(result.current.fragments).activity.status).toBe('interrupted')
  })
})

describe('流式上下文内的跨轮幽灵工具卡（结果永不到达）', () => {
  /** 一轮的输出形态：正文 + 声明卡（无结果） */
  function roundWithCard(seqBase: number): MessagePart[] {
    return [
      { type: 'text', content: '我先看一下', state: 'done', sequence: seqBase } as MessagePart,
      orphanPart({ sequence: seqBase + 1 }),
    ]
  }

  it('卡之后已有新一轮模型推送 → 仍 running（2026-11-18 裁定：流式期间不误判中断）', () => {
    const parts: MessagePart[] = [
      ...roundWithCard(0),
      // 下一轮推送（block_start 即建空 part，与 ToolTailActivity 的
      // 「模型新一轮推送到达」判据同源）
      { type: 'thinking', content: '新的一轮思考', state: 'streaming', sequence: 2 } as MessagePart,
      { type: 'text', content: '新的一轮正文', state: 'streaming', sequence: 3 } as MessagePart,
    ]
    const frag = expectToolFragment(
      buildFragmentsFromParts(makeMessage(parts), undefined, { streaming: true }),
    )
    // 裁定原文：工具仍在执行、下一轮思考已开始时，把执行中的卡误判为中断
    // 会让「结果还没到转圈就没了」。流式上下文内恒 running，终态只认
    // tool_result 到达或 run 结束（非流式上下文才收口 interrupted）。
    expect(frag.activity.status).toBe('running')
    expect(frag.activity.statusText).toBeUndefined()
  })

  it('真实合并路径：两轮 assistant 合泡后，上一轮幽灵卡与本轮尾卡都在途（流式期间不收口）', () => {
    const round1: Message = {
      ...BASE_MSG,
      id: 'a_round1',
      status: 'completed',
      parts: roundWithCard(0),
    }
    const round2: Message = {
      ...BASE_MSG,
      id: 'a_round2',
      status: 'streaming',
      parts: [
        { type: 'text', content: '第二轮正文', state: 'streaming', sequence: 0 } as MessagePart,
        {
          type: 'tool_call',
          callId: 'call_live',
          name: 'bash',
          args: {},
          state: 'calling',
          sequence: 1,
        } as ToolCallPart,
      ],
    }
    const merged = mergeConsecutiveAssistantMessages([round1, round2])
    expect(merged).toHaveLength(1)
    const { result } = renderHook(() =>
      useMessageRender({ message: merged[0], isGenerating: true, isLast: true }),
    )
    const statuses = result.current.fragments
      .filter((f) => f.type === 'tool_call')
      .map((f) => (f.type === 'tool_call' ? f.activity.status : null))
    // 2026-11-18 裁定同前：上一轮无结果的卡在流式期间同样保持 running，
    // 不因「其后有新一轮推送」的旧启发式收口 interrupted。
    expect(statuses).toEqual(['running', 'running'])
  })

  it('卡之后无任何模型推送（真在途尾卡）→ running 现状不回归', () => {
    const parts: MessagePart[] = [
      { type: 'text', content: '正文', state: 'streaming', sequence: 0 } as MessagePart,
      orphanPart({ state: 'calling', sequence: 1 }),
    ]
    const frag = expectToolFragment(
      buildFragmentsFromParts(makeMessage(parts), undefined, { streaming: true }),
    )
    expect(frag.activity.status).toBe('running')
    expect(frag.activity.statusText).toBeUndefined()
  })

  it('早期卡已有结果 → completed 优先于「其后有推送」判定', () => {
    const parts: MessagePart[] = [
      orphanPart({ state: 'done', result: '完成', sequence: 0 }),
      { type: 'text', content: '新一轮正文', state: 'streaming', sequence: 1 } as MessagePart,
    ]
    const frag = expectToolFragment(
      buildFragmentsFromParts(makeMessage(parts), undefined, { streaming: true }),
    )
    expect(frag.activity.status).toBe('completed')
    expect(frag.activity.statusText).toBeUndefined()
  })
})
