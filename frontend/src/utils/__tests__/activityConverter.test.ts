// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** activityConverter 补测：默认动作（复制参数/复制结果）与执行输出详情块 */
import { describe, expect, it, vi } from 'vitest'
import { buildDefaultActions, toolCallToActivity } from '../activityConverter'
import type { MessageToolCall } from '@/types/models'

function callOf(overrides: Partial<MessageToolCall> = {}): MessageToolCall {
  return {
    call_id: 'call-1',
    tool_name: 'bash_execute',
    tool_args: { command: 'ls' },
    status: 'completed',
    result: 'file-a\nfile-b',
    started_at: '2026-09-18T00:00:00Z',
    ...overrides,
  } as MessageToolCall
}

describe('buildDefaultActions', () => {
  it('复制参数动作：点击后把 tool_args 序列化写入剪贴板', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })
    const actions = buildDefaultActions(callOf())
    const copyArgs = actions.find((a) => a.id === 'copy_args')!
    expect(copyArgs.label).toBe('复制参数')
    await copyArgs.onClick()
    expect(writeText).toHaveBeenCalledWith(JSON.stringify({ command: 'ls' }, null, 2))
  })

  it('复制结果动作：字符串结果原样写入；对象结果 JSON 序列化', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.assign(navigator, { clipboard: { writeText } })

    const actions = buildDefaultActions(callOf())
    await actions.find((a) => a.id === 'copy_result')!.onClick()
    expect(writeText).toHaveBeenLastCalledWith('file-a\nfile-b')

    const actionsObj = buildDefaultActions(callOf({ result: { files: 2 } }))
    await actionsObj.find((a) => a.id === 'copy_result')!.onClick()
    expect(writeText).toHaveBeenLastCalledWith(JSON.stringify({ files: 2 }, null, 2))
  })

  it('无结果（result undefined）→ 只保留复制参数动作', () => {
    const actions = buildDefaultActions(callOf({ result: undefined }))
    expect(actions.map((a) => a.id)).toEqual(['copy_args'])
  })
})

describe('toolCallToActivity 详情块', () => {
  it('流式执行中的 partialOutput → 生成执行输出详情块（换行拼接）', () => {
    // 无声明工具名且 args 无 command 等启发式键 → 不触发声明级联/终端卡改写，
    // 通用详情块（args/result/output）原样保留
    const activity = toolCallToActivity(
      callOf({ tool_name: 'x', tool_args: {}, partialOutput: ['行一', '行二'] }),
    )
    const output = activity.details.find((d) => d.id === 'output')
    expect(output).toBeTruthy()
    expect(output!.content).toBe('行一\n行二')
  })

  it('无 partialOutput → 不生成执行输出块', () => {
    const activity = toolCallToActivity(callOf())
    expect(activity.details.find((d) => d.id === 'output')).toBeUndefined()
  })
})
