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
 *   的历史孤儿（无 tool 结果消息）→ interrupted。
 */
import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import {
  buildFragmentsFromParts,
  useMessageRender,
} from '@/components/chat/hooks/useMessageRender'
import { registerGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import {
  mapBackendMessageToMessage,
  mergeConsecutiveAssistantMessages,
  type BackendMessageResponse,
} from '@/services/api/session'
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
  const frag = fragments[0]
  if (frag.type !== 'tool_call') throw new Error(`expected tool_call fragment, got ${frag.type}`)
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
