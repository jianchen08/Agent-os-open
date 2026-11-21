/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * useMessageRender 截断态内容依赖卡（file_card）分支：
 *
 * file_write 结果超 8KB 截断（数据面瘦身）后，标准级联会跌落到 chat_card
 * 代码块堆砌（diff/打开入口全丢，实测根因）。截断态走 enhanceTruncatedContentCard：
 * - 详情区替换为单个一级「差异对比（展开加载全文）」块（无 junk 代码块）；
 * - 折叠 chip 元信息照常注入（标题=文件名、摘要/打开入口经 args.path 兜底）；
 * - 展开回填入口可执行（无活跃管道时如实 false）；
 * - 非 file_card 工具截断 → 仍走通用截断块（既有行为不回改）。
 *
 * mock 边界：无（hook 为纯数据装配，回填不可用路径零网络）。
 */

import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { loadRenderIntents } from '@/utils/renderIntent'
import { isContentDependentRenderCard } from '@/utils/toolCardRegistry'
import { useMessageRender } from '../useMessageRender'
import type { MessagePart } from '@/types/messageParts'
import type { Message } from '@/types/models'

const TRUNCATED_MARK = {
  __truncated: true as const,
  preview: '{"file": "/w/a.py", "old_content": "旧文',
  totalBytes: 20_480,
  sequence: 7,
}

function makeAssistantMessage(parts: MessagePart[]): Message {
  return {
    id: 'm-trunc-fc',
    sessionId: 'sess-1',
    sequence: 7,
    role: 'assistant',
    content: '',
    timestamp: '2026-11-10T00:00:00Z',
    status: 'completed',
    parts,
  } as Message
}

function fileWritePart(): MessagePart {
  return {
    type: 'tool_call',
    callId: 'c-fw',
    name: 'file_write',
    args: { path: 'src/big.py', action: 'write', content: '长文…' },
    state: 'done',
    resultData: TRUNCATED_MARK,
  } as MessagePart
}

beforeEach(() => {
  usePipelineMessageStore.setState({ activePipelineId: null } as never)
  loadRenderIntents([{ name: 'file_write', render: { card: 'file_card' } }])
})

afterEach(() => {
  loadRenderIntents([])
})

describe('useMessageRender — 截断态 file_card 分支', () => {
  it('file_write 截断 → 详情区只有一级加载块（无 chat_card 代码块堆砌），label 声明 diff 语义', () => {
    const { result } = renderHook(() =>
      useMessageRender({ message: makeAssistantMessage([fileWritePart()]), isLast: true }),
    )

    const fragment = result.current.fragments.find((f) => f.type === 'tool_call')
    expect(fragment).toBeTruthy()
    const details = (fragment as { activity: { details?: Array<{ id?: string; label: string }> } })
      .activity.details!
    expect(details).toHaveLength(1)
    expect(details[0].id).toBe('result-truncated')
    expect(details[0].label).toMatch(/^差异对比（共 \d+(\.\d+)? KB），展开加载全文$/)
    expect(JSON.stringify(details)).not.toContain('__truncated')
  })

  it('截断态折叠 chip 元信息兜底：标题=文件名、filePath 经 args.path 兜底、onOpenFile 在', () => {
    const { result } = renderHook(() =>
      useMessageRender({ message: makeAssistantMessage([fileWritePart()]), isLast: true }),
    )

    const activity = (
      result.current.fragments.find((f) => f.type === 'tool_call') as {
        activity: { title: string; subtitle?: string; filePath?: string; onOpenFile?: unknown }
      }
    ).activity
    expect(activity.title).toBe('big.py')
    expect(activity.subtitle).toBe('src/big.py')
    expect(activity.filePath).toBe('src/big.py')
    expect(activity.onOpenFile).toBeTypeOf('function')
  })

  it('加载块展开回填在无活跃管道时返回 false（不假装取到全量）', async () => {
    const { result } = renderHook(() =>
      useMessageRender({ message: makeAssistantMessage([fileWritePart()]), isLast: true }),
    )

    const block = (
      result.current.fragments.find((f) => f.type === 'tool_call') as {
        activity: { details?: Array<{ id?: string; onExpand?: () => Promise<boolean> }> }
      }
    ).activity.details?.find((d) => d.id === 'result-truncated')
    await expect(block?.onExpand?.()).resolves.toBe(false)
  })

  it('非内容依赖工具（search）截断 → 通用截断块追加，既有行为不变', () => {
    loadRenderIntents([])
    const part = {
      type: 'tool_call',
      callId: 'c-s',
      name: 'search',
      args: {},
      state: 'done',
      result: TRUNCATED_MARK,
    } as MessagePart
    const { result } = renderHook(() =>
      useMessageRender({ message: makeAssistantMessage([part]), isLast: true }),
    )

    const details = (
      result.current.fragments.find((f) => f.type === 'tool_call') as {
        activity: { details?: Array<{ id?: string; label: string }> }
      }
    ).activity.details!
    const truncated = details.filter((d) => d.id === 'result-truncated')
    expect(truncated).toHaveLength(1)
    expect(truncated[0].label).toContain('结果已截断')
  })

  it('isContentDependentRenderCard：file_card 在册 = true；未注册/其他卡型 = false', () => {
    expect(isContentDependentRenderCard('file_write')).toBe(true)
    expect(isContentDependentRenderCard('file_read')).toBe(false)
    loadRenderIntents([])
    expect(isContentDependentRenderCard('file_write')).toBe(false)
  })
})
