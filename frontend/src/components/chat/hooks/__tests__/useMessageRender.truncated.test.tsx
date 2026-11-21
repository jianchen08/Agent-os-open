/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 消息渲染 hook（useMessageRender）的截断结果分支持：
 * - 工具 part 的结果体是截断标记时，生成的 activity 详情里挂「结果已截断」块
 *   （标记对象本身不进渲染数据）；
 * - 该块的展开回填入口可执行：无活跃管道时如实返回 false（不假装已取全量）。
 *
 * mock 边界：无（hook 为纯数据装配，回填不可用路径零网络）。
 */

import { renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useMessageRender } from '../useMessageRender'
import type { MessagePart } from '@/types/messageParts'
import type { Message } from '@/types/models'

vi.mock('@/services/fileLoaderRegistry', () => ({
  getGlobalOpenFileCallback: () => vi.fn(),
}))

const TRUNCATED_MARK = {
  __truncated: true as const,
  preview: '{"rows": "预览"}',
  totalBytes: 20_480,
  sequence: 7,
}

function makeAssistantMessage(parts: MessagePart[]): Message {
  return {
    id: 'm-trunc',
    sessionId: 'sess-1',
    sequence: 7,
    role: 'assistant',
    content: '',
    timestamp: '2026-10-28T00:00:00Z',
    status: 'completed',
    parts,
  } as Message
}

function toolPart(): MessagePart {
  return {
    type: 'tool_call',
    callId: 'c1',
    name: 'search',
    args: {},
    state: 'done',
    result: TRUNCATED_MARK,
  } as MessagePart
}

beforeEach(() => {
  usePipelineMessageStore.setState({ activePipelineId: null } as never)
})

describe('useMessageRender — 截断结果分支', () => {
  it('截断标记 → activity 详情挂截断块（标记对象不上屏）', () => {
    const { result } = renderHook(() =>
      useMessageRender({ message: makeAssistantMessage([toolPart()]), isLast: true }),
    )

    const fragment = result.current.fragments.find((f) => f.type === 'tool_call')
    expect(fragment).toBeTruthy()
    const details = (fragment as { activity: { details?: Array<{ id?: string; label: string }> } })
      .activity.details
    const truncated = details?.find((d) => d.id === 'result-truncated')
    expect(truncated?.label).toContain('结果已截断')
    expect(JSON.stringify(details)).not.toContain('__truncated')
  })

  it('通用截断块不接线 onExpand（2026-11-18 裁定：展开纯渲染显示 LLM 所见，不改状态无网络）', () => {
    const { result } = renderHook(() =>
      useMessageRender({ message: makeAssistantMessage([toolPart()]), isLast: true }),
    )

    const fragment = result.current.fragments.find((f) => f.type === 'tool_call') as {
      activity: { details?: Array<{ id?: string; onExpand?: () => Promise<boolean>; label?: string }> }
    }
    const block = fragment.activity.details?.find((d) => d.id === 'result-truncated')

    expect(block?.onExpand).toBeUndefined()
    expect(block?.label).toContain('与 LLM 所见一致')
  })
})
