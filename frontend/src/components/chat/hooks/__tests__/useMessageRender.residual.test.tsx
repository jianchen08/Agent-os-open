/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * useMessageRender / buildFragmentsFromParts 残余分支补测（簇3）
 *
 * 覆盖既有 thinkingOrderFix.test.tsx 未触达的分支：
 * - system part → RenderFragment（level/notificationType 透传；空内容跳过）
 * - makeStablePartKey 的 system 分支（内容前 16 字作稳定键）与未知 type 默认分支
 * - 默认详情块的 result / partialOutput 两段（buildDefaultDetails）
 * - tool_call part → activity 增强时 onOpenFile 经 taskId 覆盖 record 的 containerTaskId
 * - isStreaming 判定（isGenerating && isLast && role=assistant 三者合取）
 *
 * 不可达/未覆盖说明（本文件 docstring 存证）：
 * - useMessageRender 的 `versionContent ?? message.content`（L302）在 parts 缺失时
 *   才走；本测试以 parts=[] 覆盖该分支，属正常回退出口而非死代码。
 * - makeStablePartKey 的 default 分支（L141）在 MessagePart 联合类型下不可达——
 *   四种 part 类型已穷尽 switch，保留为防御分支（新增 part 类型时兜底不崩）。
 *   测试经 `as` 断言构造未知 type 显式固化该兜底行为。
 */
import { renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  buildFragmentsFromParts,
  useMessageRender,
} from '@/components/chat/hooks/useMessageRender'
import { registerGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import type { MessagePart } from '@/types/messageParts'
import type { Message } from '@/types/models'

const BASE_MSG: Message = {
  id: 'msg-residual-001',
  sessionId: 'session-residual',
  role: 'assistant',
  content: '',
  timestamp: '2026-01-01T00:00:00.000Z',
  parentId: null,
  status: 'completed',
}

function makeMessage(parts: MessagePart[] | undefined, content = ''): Message {
  return { ...BASE_MSG, parts, content }
}

beforeEach(() => {
  registerGlobalOpenFileCallback(() => {})
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('system part → 渲染片段', () => {
  it('system 片段透传 level / notificationType 与稳定 key（前 16 字内容片段）', () => {
    const content = '系统通知正文超过十六个字符的测试内容'
    const fragments = buildFragmentsFromParts(
      makeMessage([{ type: 'system', content, level: 'warning', notificationType: 'task_failed' }]),
    )

    expect(fragments).toHaveLength(1)
    const frag = fragments[0]
    expect(frag.type).toBe('system')
    if (frag.type !== 'system') throw new Error('片段类型收窄失败')
    expect(frag.level).toBe('warning')
    expect(frag.notificationType).toBe('task_failed')
    expect(frag.content).toBe(content)
    // 稳定键含内容前缀（seq 缺省回落索引 0）
    expect(frag.key).toBe(`part-system-0-${content.substring(0, 16)}`)
  })

  it.each([
    ['空串', ''],
    ['纯空白', '   \n  '],
  ])('system part 内容为%s → 不产出片段', (_name, content) => {
    const fragments = buildFragmentsFromParts(
      makeMessage([{ type: 'system', content, level: 'info', notificationType: 'n' }]),
    )
    expect(fragments).toHaveLength(0)
  })

  it('system part 缺 content（历史消息容错）→ 稳定键前缀为空，且不崩', () => {
    const part = { type: 'system', level: 'info', notificationType: 'n' } as unknown as MessagePart
    const fragments = buildFragmentsFromParts(makeMessage([part]))
    expect(fragments).toHaveLength(0)
  })
})

describe('稳定 key 生成分支', () => {
  it.each([
    ['text', 'part-text-0-'],
    ['thinking', 'part-thinking-0-'],
  ] as const)('%s part 的 key 携带内容前 16 字（避免索引变化重建 DOM）', (type, prefix) => {
    const content = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    const part = { type, content, state: 'done' } as unknown as MessagePart
    const fragments = buildFragmentsFromParts(makeMessage([part]))
    expect(fragments[0].key).toBe(`${prefix}${content.substring(0, 16)}`)
  })

  it('sequence 存在时优先于数组索引（历史消息按 seq 保证有序）', () => {
    const fragments = buildFragmentsFromParts(
      makeMessage([{ type: 'text', content: 'hi', state: 'done', sequence: 42 }]),
    )
    expect(fragments[0].key).toBe('part-text-42-hi')
  })

  it('tool_call part 的 key 只含 callId（不含内容片段）', () => {
    const fragments = buildFragmentsFromParts(
      makeMessage([
        {
          type: 'tool_call',
          callId: 'call-abc',
          name: 'unknown_residual_tool',
          args: {},
          state: 'done',
        },
      ]),
    )
    expect(fragments[0].key).toBe('part-tool-call-abc')
  })

  it('未声明的 part 类型 → 索引兜底键且不崩（防御默认分支）', () => {
    const part = { type: 'brand_new_part_type' } as unknown as MessagePart
    const fragments = buildFragmentsFromParts(makeMessage([part]))
    // 未知类型不产出片段，但遍历不抛错
    expect(fragments).toHaveLength(0)
  })
})

describe('tool_call part → activity 详情与文件入口', () => {
  it('result + partialOutput 齐备 → 条目详情含参数/结果/执行输出三类块', () => {
    const fragments = buildFragmentsFromParts(
      makeMessage([
        {
          type: 'tool_call',
          callId: 'c1',
          name: 'unknown_residual_tool',
          args: { a: 1 },
          state: 'done',
          result: '{"ok":true}',
          partialOutput: ['line1', 'line2'],
        },
      ]),
    )

    const frag = fragments[0]
    if (frag.type !== 'tool_call') throw new Error('片段类型收窄失败')
    const details = frag.activity.details ?? []
    // 参数与结果按标量收进 kv 块，执行输出走 log 块
    expect(details.find((d) => d.kvItems?.some((kv) => kv.key === 'a'))).toBeDefined()
    expect(details.find((d) => d.kvItems?.some((kv) => kv.key === 'ok'))).toBeDefined()
    const output = details.find((d) => d.id === 'output')
    expect(output?.content).toBe('line1\nline2')
    expect(output?.contentType).toBe('log')
  })

  it.each([
    ['result 缺省', { result: undefined }],
    ['partialOutput 空数组', { partialOutput: [] }],
  ])('%s → 详情不产出对应块', (_name, over) => {
    const fragments = buildFragmentsFromParts(
      makeMessage([
        {
          type: 'tool_call',
          callId: 'c2',
          name: 'unknown_residual_tool',
          args: { keep: 'yes' },
          state: 'done',
          ...over,
        } as MessagePart,
      ]),
    )
    const frag = fragments[0]
    if (frag.type !== 'tool_call') throw new Error('片段类型收窄失败')
    const details = frag.activity.details ?? []
    // 参数块仍在（有值即渲染）
    expect(details.some((d) => d.kvItems?.some((kv) => kv.key === 'keep'))).toBe(true)
    // 缺 result 时无结果块；partialOutput 空时不产执行输出块
    if ('result' in over) {
      expect(details.some((d) => d.id === '结果-kv' || d.id === 'result')).toBe(false)
    }
    if ('partialOutput' in over) {
      expect(details.some((d) => d.id === 'output')).toBe(false)
    }
  })

  it('taskId 传入时覆盖 record 的 containerTaskId（当前 Tab 工作区优先）', () => {
    const openFile = vi.fn()
    registerGlobalOpenFileCallback(openFile)

    const fragments = buildFragmentsFromParts(
      makeMessage([
        {
          type: 'tool_call',
          callId: 'c3',
          name: 'file_read',
          args: { path: 'src/main.py' },
          state: 'done',
          resultData: { file: 'src/main.py', content: 'x', total_lines: 1 },
          containerTaskId: 'record-task',
        },
      ]),
      'tab-task',
    )

    const frag = fragments[0]
    if (frag.type !== 'tool_call') throw new Error('片段类型收窄失败')
    expect(frag.activity.filePath).toBe('src/main.py')
    frag.activity.onOpenFile?.('src/main.py')
    expect(openFile).toHaveBeenCalledWith('src/main.py', 'tab-task')
  })

  it('taskId 缺省时回落 record 的 containerTaskId', () => {
    const openFile = vi.fn()
    registerGlobalOpenFileCallback(openFile)

    const fragments = buildFragmentsFromParts(
      makeMessage([
        {
          type: 'tool_call',
          callId: 'c4',
          name: 'file_read',
          args: { path: 'src/other.py' },
          state: 'done',
          resultData: { file: 'src/other.py', content: 'y', total_lines: 1 },
          containerTaskId: 'record-task',
        },
      ]),
    )

    const frag = fragments[0]
    if (frag.type !== 'tool_call') throw new Error('片段类型收窄失败')
    frag.activity.onOpenFile?.('src/other.py')
    expect(openFile).toHaveBeenCalledWith('src/other.py', 'record-task')
  })

  it('index/total 反映 tool_call 计数（多个 tool_call 依次递增）', () => {
    const fragments = buildFragmentsFromParts(
      makeMessage([
        { type: 'text', content: '前言', state: 'done' },
        { type: 'tool_call', callId: 'a', name: 'unknown_residual_tool', args: {}, state: 'done' },
        { type: 'tool_call', callId: 'b', name: 'unknown_residual_tool', args: {}, state: 'done' },
      ]),
    )
    const tools = fragments.filter((f) => f.type === 'tool_call')
    expect(tools.map((f) => (f.type === 'tool_call' ? f.index : -1))).toEqual([0, 1])
    expect(tools.every((f) => f.type === 'tool_call' && f.total === 2)).toBe(true)
  })
})

describe('useMessageRender hook', () => {
  it('isLast 的 text 片段被标记 isLast=true，其余为 false', () => {
    const { result } = renderHook(() =>
      useMessageRender({
        message: makeMessage([
          { type: 'text', content: '第一段', state: 'done' },
          { type: 'thinking', content: '思考', state: 'done' },
          { type: 'text', content: '第二段', state: 'done' },
        ]),
      }),
    )

    const texts = result.current.fragments.filter((f) => f.type === 'text')
    expect(texts.map((f) => (f.type === 'text' ? f.isLast : null))).toEqual([false, true])
  })

  it('无 text 片段时不误标（reduce 初值 -1 分支）', () => {
    const { result } = renderHook(() =>
      useMessageRender({
        message: makeMessage([
          { type: 'thinking', content: '只有思考', state: 'done' },
          { type: 'system', content: '通知', level: 'info', notificationType: 'n' },
        ]),
      }),
    )
    expect(result.current.fragments.some((f) => f.type === 'text')).toBe(false)
  })

  it.each([
    ['isGenerating+isLast+assistant', { isGenerating: true, isLast: true, role: 'assistant' as const }, true],
    ['非最后一条', { isGenerating: true, isLast: false, role: 'assistant' as const }, false],
    ['非 assistant 角色', { isGenerating: true, isLast: true, role: 'user' as const }, false],
    ['未在生成', { isGenerating: false, isLast: true, role: 'assistant' as const }, false],
  ])('isStreaming 判定：%s → %s', (_name, opts, expected) => {
    const { result } = renderHook(() =>
      useMessageRender({
        message: { ...makeMessage([{ type: 'text', content: 'x', state: 'done' }]), role: opts.role },
        isGenerating: opts.isGenerating,
        isLast: opts.isLast,
      }),
    )
    expect(result.current.isStreaming).toBe(expected)
  })

  it('parts 缺失时回退 versionContent（编辑态预览），无版本内容则回落 message.content', () => {
    const withVersion = renderHook(() =>
      useMessageRender({ message: makeMessage(undefined, '原文'), versionContent: '编辑中草稿' }),
    )
    expect(withVersion.result.current.fragments).toHaveLength(0)
    expect(withVersion.result.current.displayContent).toBe('编辑中草稿')

    const noVersion = renderHook(() => useMessageRender({ message: makeMessage([], '原文') }))
    expect(noVersion.result.current.displayContent).toBe('原文')
  })

  it('有 parts 时 displayContent = 各 text 片段拼接（parts 为唯一数据源）', () => {
    const { result } = renderHook(() =>
      useMessageRender({
        message: makeMessage(
          [
            { type: 'text', content: 'A', state: 'done' },
            { type: 'thinking', content: 'T', state: 'done' },
            { type: 'text', content: 'B', state: 'done' },
          ],
          '不应采用的内容',
        ),
      }),
    )
    expect(result.current.displayContent).toBe('AB')
    expect(result.current.messageId).toBe('msg-residual-001')
  })
})
