/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/** @feature call_tool 动态转发目标工具展示 | @ci: frontend-test */
/**
 * call_tool 动态转发工具卡展示回归（2026-11-18 用户裁定：前端展示实际被
 * 调用的目标工具，不是调用工具的工具）。
 *
 * 三组验收（任务书判据，断行为不断实现）：
 * (1) 结果带目标名（metadata.target_tool 契约键优先 / 壳成功路径合并的
 *     tool_name / 失败信封回退声明侧 args.tool）→ 标题显示目标名 + 转发标注，
 *     参数区按目标实参展开（信封 {tool, arguments} 不进卡面）；
 * (2) 在途无结果 / 非 call_tool → 保持现状渲染（壳名标题、无转发标注）；
 * (3) 失败信封（壳自身失败 error / 壳成功内层 success=false）→ 标题仍显示
 *     目标名。
 * 2026-11-19 用户裁定追加：目标名卡头部渲染「间接调用」徽标（indirectCall
 * 置位 → ActivityCard data-testid=indirect-call-badge）；在途/原生卡无徽标。
 *
 * 数据面事实（前端可观测形态）：目标名经 resultData.metadata.target_tool
 * （契约键，上行投影到位时命中）或 resultData.tool_name（壳成功路径合并）
 * 到达；失败信封不带目标名，唯一来源是 args.tool。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import ActivityCard from '@/components/chat/ActivityCard'
import { buildFragmentsFromParts } from '@/components/chat/hooks/useMessageRender'
import { toolCallToActivity } from '@/utils/activityConverter'
import { registerGlobalOpenFileCallback } from '@/utils/toolCardRegistry'
import type { ToolCallPart } from '@/types/messageParts'
import type { Message, MessageToolCall } from '@/types/models'

const FORWARD_ANNOTATION = '经 call_tool 动态转发'
const INDIRECT_BADGE = '间接调用'

/** 壳层入参信封：LLM 调 call_tool 的原样参数 */
const ENVELOPE_ARGS = { tool: 'snippet_box', arguments: { keyword: '温度', scope: 'today' } }

function makeForwardCall(overrides?: Partial<MessageToolCall>): MessageToolCall {
  return {
    call_id: 'call_dyn_1',
    tool_name: 'call_tool',
    tool_args: ENVELOPE_ARGS,
    status: 'completed',
    result: '{"hits": ["a"], "tool_name": "snippet_box"}',
    // 壳成功路径的当前可观测形态：目标输出 + 壳合并的 tool_name
    resultData: { hits: ['a'], tool_name: 'snippet_box' },
    duration_ms: 42,
    ...overrides,
  }
}

function renderForwardedCard(call: MessageToolCall) {
  const activity = toolCallToActivity(call)
  render(<ActivityCard activity={activity} />)
  return activity
}

beforeEach(() => {
  registerGlobalOpenFileCallback(() => {})
})

describe('call_tool 动态转发：目标名可达（成功信封）', () => {
  it('(1a) resultData.tool_name（壳合并形态）→ 标题=目标名+转发标注，参数按目标实参展开', () => {
    const activity = renderForwardedCard(makeForwardCall())
    expect(activity.title).toBe('snippet_box')
    expect(activity.subtitle).toBe(FORWARD_ANNOTATION)

    // DOM：标题显示目标名（原始名），壳名（L0 人性化 Call Tool）不出现在标题位
    expect(screen.getByText('snippet_box')).toBeInTheDocument()
    expect(screen.queryByText('Call Tool')).toBeNull()
    expect(screen.getByText(FORWARD_ANNOTATION)).toBeInTheDocument()

    // 2026-11-19 裁定：头部「间接调用」徽标在场（数据位 + DOM 双断言）
    expect(activity.indirectCall).toBe(true)
    expect(screen.getByTestId('indirect-call-badge')).toBeInTheDocument()
    expect(screen.getByText(INDIRECT_BADGE)).toBeInTheDocument()

    // 展开：参数区显示目标实参键值，信封键（tool/arguments）不进卡面
    fireEvent.click(screen.getByText('snippet_box'))
    expect(screen.getByText('keyword')).toBeInTheDocument()
    expect(screen.getByText('温度')).toBeInTheDocument()
    expect(screen.queryByText('arguments')).toBeNull()
    // 结果区：目标输出可见（hits 键来自透传体）
    expect(screen.getByText('hits')).toBeInTheDocument()
  })

  it('(1b) resultData.metadata.target_tool 契约键优先于 tool_name（同名冲突时取前者）', () => {
    const activity = renderForwardedCard(
      makeForwardCall({
        resultData: { metadata: { target_tool: 'tool_a' }, tool_name: 'tool_b' },
      }),
    )
    expect(activity.title).toBe('tool_a')
    expect(screen.getByText('tool_a')).toBeInTheDocument()
  })

  it('(1c) parts 渲染路径（useMessageRender fragments）同构改写', () => {
    const part: ToolCallPart = {
      type: 'tool_call',
      callId: 'call_dyn_2',
      name: 'call_tool',
      args: ENVELOPE_ARGS,
      state: 'done',
      result: '{"hits": ["a"], "tool_name": "snippet_box"}',
      resultData: { hits: ['a'], tool_name: 'snippet_box' },
      sequence: 0,
    }
    const message: Message = {
      id: 'msg_dyn',
      sessionId: 'sess',
      role: 'assistant',
      content: '',
      timestamp: '2026-11-18T10:00:00.000Z',
      parts: [part],
    }
    const fragments = buildFragmentsFromParts(message, undefined, { streaming: false })
    const frag = fragments.find((f) => f.type === 'tool_call')
    expect(frag).toBeDefined()
    if (frag?.type !== 'tool_call') throw new Error('expected tool_call fragment')
    expect(frag.activity.title).toBe('snippet_box')
    expect(frag.activity.subtitle).toBe(FORWARD_ANNOTATION)
  })

  it('(1d) 声明侧缺 arguments 键（目标无参调用）→ 标题仍目标名、参数区空不报错', () => {
    const activity = renderForwardedCard(
      makeForwardCall({
        tool_args: { tool: 'list_all' },
        result: '{"items": []}',
        resultData: { items: [] },
      }),
    )
    expect(activity.title).toBe('list_all')
    expect(activity.subtitle).toBe(FORWARD_ANNOTATION)
  })
})

describe('call_tool 动态转发：名字不可得 → 保持现状渲染', () => {
  it('(2a) 在途无结果 → 壳名标题、无转发标注、参数区是信封原样', () => {
    const activity = renderForwardedCard(
      makeForwardCall({ status: 'running', result: undefined, resultData: undefined, duration_ms: undefined }),
    )
    expect(activity.title).toBe('Call Tool')
    expect(activity.subtitle).toBeUndefined()
    expect(screen.getByText('Call Tool')).toBeInTheDocument()
    expect(screen.queryByText(FORWARD_ANNOTATION)).toBeNull()
    // 在途卡无间接调用徽标（目标名未定，徽标不提前出现）
    expect(activity.indirectCall).toBeUndefined()
    expect(screen.queryByTestId('indirect-call-badge')).toBeNull()

    // 展开：参数区保持信封（arguments 信封键在场——现状渲染不回归）
    fireEvent.click(screen.getByText('Call Tool'))
    expect(screen.getByText('arguments')).toBeInTheDocument()
  })

  it('(2b) 非 call_tool 工具（原生调用）不受改写：即使结果带 tool_name 键', () => {
    const activity = toolCallToActivity({
      call_id: 'call_native',
      tool_name: 'snippet_box',
      tool_args: { keyword: '温度' },
      status: 'completed',
      result: '{"hits": ["a"]}',
      resultData: { hits: ['a'], tool_name: 'snippet_box' },
    })
    expect(activity.title).toBe('Snippet Box')
    // 副标题是 L0 通用摘要（如 args.keyword 值），不是转发标注——原生卡零改写
    expect(activity.subtitle).not.toBe(FORWARD_ANNOTATION)
  })
})

describe('call_tool 动态转发：失败信封也显示目标名', () => {
  it('(3a) 壳自身失败（TOOL_NOT_FOUND，无结果体）→ 标题=声明侧目标名，错误可见', () => {
    const activity = renderForwardedCard(
      makeForwardCall({
        status: 'failed',
        result: undefined,
        resultData: undefined,
        error: "Error: TOOL_EXEC_FAILED: 目标工具 'snippet_box' 不在本轮动态工具面",
      }),
    )
    expect(activity.title).toBe('snippet_box')
    expect(activity.subtitle).toBe(FORWARD_ANNOTATION)
    expect(screen.getByText('snippet_box')).toBeInTheDocument()
    // 失败卡同样带间接调用徽标（转发链路死了也是间接调用）
    expect(activity.indirectCall).toBe(true)
    expect(screen.getByTestId('indirect-call-badge')).toBeInTheDocument()

    // 展开：目标实参在场、错误文本可见（失败详情不因改写丢失）
    fireEvent.click(screen.getByText('snippet_box'))
    expect(screen.getByText('keyword')).toBeInTheDocument()
    expect(screen.getByText(/不在本轮动态工具面/)).toBeInTheDocument()
  })

  it('(3b) 壳成功内层目标失败（success=false 信封无 tool_name）→ 标题回退声明侧目标名', () => {
    const activity = renderForwardedCard(
      makeForwardCall({
        result: '{"success": false, "error": "sidecar 进程已死", "error_code": "PLUGIN_CRASHED"}',
        resultData: { success: false, error: 'sidecar 进程已死', error_code: 'PLUGIN_CRASHED' },
      }),
    )
    expect(activity.title).toBe('snippet_box')
    expect(activity.subtitle).toBe(FORWARD_ANNOTATION)

    // 展开：内层失败信封字段可见（目标的真实死因，不被壳改写吞掉）
    fireEvent.click(screen.getByText('snippet_box'))
    expect(screen.getByText('PLUGIN_CRASHED')).toBeInTheDocument()
    expect(screen.getByText(/sidecar 进程已死/)).toBeInTheDocument()
  })
})
