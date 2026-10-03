// @feature FP-T12 前端适配 | @ci: frontend-test
/**
 * messageCardRouter 路由特征化测试（分发器核心，改造护栏）：
 * - role=tool → tool-card（无其他门，优先级最高）
 * - 非流式 assistant/system 携带已声明 message_style → style-card
 * - 流式不路由（正文仍在到达）；声明缺席（禁用/未声明）→ fallback
 * - 其余（user、无样式）→ null（fallback 骨架）
 */
import { beforeEach, describe, expect, it } from 'vitest'
import { resolveMessageCardRoute } from '../messageCardRouter'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import type { Message } from '@/types/models'

function makeMessage(overrides: Partial<Message> = {}): Message {
  return {
    id: 'm-1',
    sessionId: 's-1',
    sequence: 1,
    role: 'assistant',
    content: 'hello',
    timestamp: new Date().toISOString(),
    status: 'completed',
    ...overrides,
  }
}

beforeEach(() => {
  contributionRegistry.clear()
  contributionRegistry.registerFromSchema({
    plugin_contributes: [
      {
        plugin_id: 'test_card_plugin',
        plugin_name: '测试消息卡',
        contributes: {
          chatMessages: [
            { id: 'my_card', title: '测试卡', props: { htmlPath: '/webview/my_card.html' } },
          ],
        },
      },
    ],
  } as never)
})

describe('messageCardRouter — 消息卡路由', () => {
  it('role=tool → tool-card（无流式/样式门，优先级最高）', () => {
    expect(resolveMessageCardRoute(makeMessage({ role: 'tool', status: 'streaming' }))).toEqual({
      kind: 'tool-card',
    })
  })

  it('非流式 assistant/system 携带已声明样式 → style-card', () => {
    expect(
      resolveMessageCardRoute(makeMessage({ metadata: { message_style: 'my_card' } })),
    ).toEqual({ kind: 'style-card', styleId: 'my_card' })
    expect(
      resolveMessageCardRoute(
        makeMessage({ role: 'system', metadata: { message_style: 'my_card' } }),
      ),
    ).toEqual({ kind: 'style-card', styleId: 'my_card' })
  })

  it('流式 assistant 携带样式 → 不路由（正文仍在到达）', () => {
    expect(
      resolveMessageCardRoute(
        makeMessage({ status: 'streaming', metadata: { message_style: 'my_card' } }),
      ),
    ).toBeNull()
  })

  it('声明缺席 / 样式非字符串 / user 消息 → fallback', () => {
    // 插件禁用（registry 无此样式声明）同源消失
    expect(resolveMessageCardRoute(makeMessage({ metadata: { message_style: 'nope' } }))).toBeNull()
    expect(resolveMessageCardRoute(makeMessage({ metadata: { message_style: 123 } }))).toBeNull()
    expect(
      resolveMessageCardRoute(makeMessage({ role: 'user', metadata: { message_style: 'my_card' } })),
    ).toBeNull()
    expect(resolveMessageCardRoute(makeMessage())).toBeNull()
  })
})
