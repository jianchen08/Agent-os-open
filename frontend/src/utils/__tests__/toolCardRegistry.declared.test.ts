/**
 * 功能测试：enhanceActivityWithToolConfig 声明优先（chat_card 注入点端到端）
 *
 * 功能点：工具调用带 ui.chat_card 声明（toolCall.chat_card，后端透传）→ enhance 走解释器
 * → 产出 ActivityData.details 命中声明的块（而非手写 registry 或 L0 推断）。
 * 无声明时回退既有路径（不回归）。
 */

import { describe, expect, it } from 'vitest'
import { enhanceActivityWithToolConfig } from '@/utils/toolCardRegistry'
import type { ChatCardDeclaration } from '@/utils/chatCardInterpreter'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

function makeActivity(toolName: string): ActivityData {
  return { type: 'tool_call', toolName, title: toolName, status: 'completed' } as ActivityData
}

function makeToolCall(overrides: Partial<MessageToolCall>): MessageToolCall {
  return {
    id: 'tc1',
    tool: 'x',
    tool_args: {},
    status: 'completed',
    ...overrides,
  } as unknown as MessageToolCall
}

describe('功能点：enhanceActivityWithToolConfig 声明优先（chat_card 注入）', () => {
  it('toolCall 带 chat_card 声明 → 标题模板与 details 来自解释器', () => {
    const decl: ChatCardDeclaration = {
      title: '{{args.q | truncate:6}}',
      blocks: [
        { type: 'kv', label: '结果', fields: [{ key: '译文', source: 'args.target' }] },
        { type: 'link', label: '来源', source: 'args.url' },
      ],
    }
    const activity = makeActivity('translate')
    const toolCall = makeToolCall({
      tool: 'translate',
      tool_args: { q: 'long query text here', target: '你好', url: 'https://x.com' },
      // 后端透传的声明字段（MessageToolCall 暂未类型化，运行期存在）
      ...({ chat_card: decl } as object),
    })

    const out = enhanceActivityWithToolConfig(activity, toolCall)

    expect(out.title).toBe('long q…')
    expect(out.details).toHaveLength(2)
    expect(out.details[0].contentType).toBe('kv')
    expect(out.details[0].kvItems).toEqual([{ key: '译文', value: '你好' }])
    expect(out.details[1].contentType).toBe('link')
    expect(out.details[1].url).toBe('https://x.com')
  })

  it('无 chat_card 声明 → 回退既有路径（手写 registry 或 L0 推断），不回归', () => {
    const activity = makeActivity('unknown_tool_xyz')
    const toolCall = makeToolCall({ tool: 'unknown_tool_xyz', tool_args: { a: 1 } })

    const out = enhanceActivityWithToolConfig(activity, toolCall)

    // 无声明 + 无手写配置 → L0 推断（标题人性化）
    expect(out.title).toBeTruthy()
    expect(out.details).toBeDefined()
  })
})
