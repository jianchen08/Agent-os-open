/**
 * 功能测试：enhanceActivityWithToolConfig 声明优先（chat_card 注入点端到端）
 *
 * 功能点：插件在 manifest 的 capabilities.tools[].ui.chat_card 声明卡片 → 后端经
 * /api/v1/schema 的 tools[].ui.chat_card 透传 → 前端 schema 加载时入注册表 →
 * enhance 按 toolName 查到声明 → 解释器产出 details。无声明回退既有路径（不回归）。
 *
 * 本测试模拟 schema 装载（loadChatCardDeclarations）+ 真实 enhance 调用，端到端验证。
 */

import { afterEach, describe, expect, it } from 'vitest'
import { clearChatCardDeclarations, loadChatCardDeclarations } from '@/utils/chatCardInterpreter'
import type { ChatCardDeclaration } from '@/utils/chatCardInterpreter'
import { enhanceActivityWithToolConfig } from '@/utils/toolCardRegistry'
import type { ActivityData } from '@/types/activity'
import type { MessageToolCall } from '@/types/models'

afterEach(() => clearChatCardDeclarations())

function makeActivity(toolName: string): ActivityData {
  return { type: 'tool_call', toolName, title: toolName, status: 'completed' } as ActivityData
}

function makeToolCall(tool: string, args: Record<string, unknown>): MessageToolCall {
  return { id: 'tc1', tool, tool_args: args, status: 'completed' } as unknown as MessageToolCall
}

describe('功能点：enhanceActivityWithToolConfig 声明优先（chat_card 按 toolName 查注册表）', () => {
  it('schema 装载的 chat_card 声明 → enhance 走解释器产出标题与 details', () => {
    const decl: ChatCardDeclaration = {
      title: '{{args.q | truncate:6}}',
      blocks: [
        { type: 'kv', label: '结果', fields: [{ key: '译文', source: 'args.target' }] },
        { type: 'link', label: '来源', source: 'args.url' },
      ],
    }
    // 模拟 /api/v1/schema 的 tools 字段经 GrowthLoop 装载
    loadChatCardDeclarations([{ name: 'translate', ui: { chat_card: decl } }])

    const out = enhanceActivityWithToolConfig(
      makeActivity('translate'),
      makeToolCall('translate', { q: 'long query text here', target: '你好', url: 'https://x.com' }),
    )

    expect(out.title).toBe('long q…')
    expect(out.details).toHaveLength(2)
    expect(out.details[0].contentType).toBe('kv')
    expect(out.details[0].kvItems).toEqual([{ key: '译文', value: '你好' }])
    expect(out.details[1].contentType).toBe('link')
    expect(out.details[1].url).toBe('https://x.com')
  })

  it('未声明 chat_card 的工具 → 回退手写 registry / L0 推断，不回归', () => {
    // 注册表为空（无声明）
    const out = enhanceActivityWithToolConfig(
      makeActivity('unknown_tool_xyz'),
      makeToolCall('unknown_tool_xyz', { a: 1 }),
    )
    expect(out.title).toBeTruthy()
    expect(out.details).toBeDefined()
  })

  it('声明按 toolName 精确匹配（其他工具不误用）', () => {
    loadChatCardDeclarations([{ name: 'translate', ui: { chat_card: { title: '翻译' } } }])
    const out = enhanceActivityWithToolConfig(
      makeActivity('other_tool'),
      makeToolCall('other_tool', {}),
    )
    // other_tool 无声明 → 不应拿到 translate 的标题
    expect(out.title).not.toBe('翻译')
  })

  it('声明的 icon 字符串 → 解析为图标组件并设入 customIcon', () => {
    loadChatCardDeclarations([
      { name: 'bash_tool', ui: { chat_card: { icon: 'terminal', title: '命令' } } },
    ])
    const out = enhanceActivityWithToolConfig(
      makeActivity('bash_tool'),
      makeToolCall('bash_tool', {}),
    )
    // customIcon 被设置（图标组件渲染为 ReactNode，非空）
    expect(out.customIcon).toBeTruthy()
  })
})
