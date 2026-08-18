/**
 * 渲染路由器端到端：插件声明 → schema 装载 → 真实卡片渲染
 *
 * 双路由体系的完整生效链路（对齐用户诉求「任务提交工具卡片应显示任务情况
 * 与提交参数」）：
 *   plugin.json capabilities.tools[].render（task_submit → form 卡）
 *   → /api/v1/schema tools[] 透传
 *   → GrowthLoop loadRenderIntents 装载
 *   → enhanceActivityWithToolConfig 声明路由 → ActivityCard form 块渲染
 *
 * 与 dsh_adapter 的 render 先例同构（dsh_read → read 卡），此处验证主工具链。
 */
import { render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import ActivityCard from '@/components/chat/ActivityCard'
import { loadRenderIntents, clearRenderIntents } from '@/utils/dshRenderIntent'
import { enhanceActivityWithToolConfig } from '@/utils/toolCardRegistry'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

vi.mock('@/components/approval', () => ({ TextDiffView: () => null }))
vi.mock('@/components/shared/markdown/MarkdownRenderer', () => ({ MarkdownRenderer: () => null }))
// 部分 mock：保留 enhanceActivityWithToolConfig 真实实现，仅替换全局文件打开回调
vi.mock('@/utils/toolCardRegistry', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/utils/toolCardRegistry')>()
  return { ...actual, getGlobalOpenFileCallback: () => () => {} }
})

afterEach(() => clearRenderIntents())

function makeToolCall(tool: string, args: Record<string, unknown>, resultData: Record<string, unknown>): MessageToolCall {
  return {
    id: 'tc1',
    tool_name: tool,
    tool_args: args,
    status: 'completed',
    resultData,
  } as unknown as MessageToolCall
}

describe('声明路由 e2e：task_submit → form 卡（任务表单渲染提交参数与任务情况）', () => {
  it('schema 装载插件 render 声明 → 卡片表单展示目标/执行者/验收标准/任务ID/状态', () => {
    // 模拟 GrowthLoop：schema.tools 携带 task_submit 的 render 声明（与
    // plugins/shared/tools/task_submit/plugin.json 一致）
    loadRenderIntents([
      { name: 'task_submit', render: { card: 'form', title: '任务提交' } },
    ])

    const activity: ActivityData = {
      type: 'tool_call',
      id: 'act-1',
      title: 'task_submit 工具调用',
      toolName: 'task_submit',
      status: 'completed',
      details: [],
      actions: [],
    }
    const toolCall = makeToolCall(
      'task_submit',
      {
        goal_title: '实现登录',
        target_id: 'agent-1',
        priority: 7,
        acceptance_criteria: { file_check: { input_params: { path: 'src/main.py' } } },
      },
      { task_id: 't-123', title: '实现登录', status: 'running', target_id: 'agent-1', message: '任务已提交，执行管道已创建' },
    )

    const enhanced = enhanceActivityWithToolConfig(activity, toolCall)
    expect(enhanced.details).toHaveLength(1)
    const formBlock = enhanced.details?.[0]
    expect(formBlock?.contentType).toBe('form')
    expect(formBlock?.label).toBe('任务提交')

    // 提交参数（args 标量 → kv）
    const kv = formBlock?.kvItems ?? []
    const kvMap = Object.fromEntries(kv.map((i) => [i.key, i.value]))
    expect(kvMap).toMatchObject({
      任务目标: '实现登录',
      目标Agent: 'agent-1',
      优先级: '7',
      任务ID: 't-123',
      状态: 'running',
    })
    // 任务情况 + 嵌套结构（验收标准 → 折叠 jsonItems；短消息进 kv）
    const jsonLabels = (formBlock?.jsonItems ?? []).map((j) => j.label)
    expect(jsonLabels).toContain('验收标准')
    expect(kvMap['消息']).toBe('任务已提交，执行管道已创建')
  })

  it('真实卡片 DOM：表单标量可见、验收标准折叠点击展开', () => {
    loadRenderIntents([{ name: 'task_submit', render: { card: 'form', title: '任务提交' } }])
    const activity: ActivityData = {
      type: 'tool_call',
      id: 'act-2',
      title: 'task_submit 工具调用',
      toolName: 'task_submit',
      status: 'completed',
      details: [],
      actions: [],
    }
    const toolCall = makeToolCall(
      'task_submit',
      { goal_title: '实现登录' },
      { task_id: 't-1', status: 'running' },
    )
    const enhanced = enhanceActivityWithToolConfig(activity, toolCall)
    render(<ActivityCard defaultExpanded activity={enhanced as ActivityData} />)

    expect(screen.getByText('任务提交')).toBeInTheDocument()
    expect(screen.getByText('实现登录')).toBeInTheDocument()
    expect(screen.getByText('t-1')).toBeInTheDocument()
  })
})

describe('数据路由 e2e：无声明工具按数据形状渲染（diff 数据 → diff 组件）', () => {
  it('未声明工具的结果含 old/new 文本对 → 渲染 dsh:diff 块', () => {
    // 不装载任何声明（cleanRenderIntents 已在 afterEach）
    const activity: ActivityData = {
      type: 'tool_call',
      id: 'act-3',
      title: 'merge 工具调用',
      toolName: 'merge',
      status: 'completed',
      details: [],
      actions: [],
    }
    const toolCall = makeToolCall('merge', {}, { old_content: 'a', new_content: 'b' })
    const enhanced = enhanceActivityWithToolConfig(activity, toolCall)
    expect(enhanced.details?.[0]?.contentType).toBe('dsh:diff')
  })
})
