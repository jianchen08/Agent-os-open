/**
 * 后端消息映射 containerTaskId 字段回归测试
 *
 * 背景：session.ts 曾读 snake_case 字段 tc.container_task_id，
 * 但 tc 由 camelCase 构建（containerTaskId），导致历史消息恢复后
 * tool_call part 的 containerTaskId 恒为 undefined，
 * 工具卡片"打开文件"无法解析工作空间而报"路径超出工作空间范围"。
 *
 * 此测试锁住字段映射，防止再次回归到 snake_case。
 */

import { describe, it, expect } from 'vitest'
import { mapBackendMessageToMessage } from '@/services/api/session'
import type { ToolCallPart } from '@/types/messageParts'

describe('mapBackendMessageToMessage - containerTaskId 字段映射', () => {
  it('历史消息 tool_call part 的 containerTaskId 应正确取自后端 camelCase 字段', () => {
    // 后端 ToolCallItem 模型只产出 camelCase 字段（见 src/channels/api/models.py）
    const backendMessage = {
      id: 'msg-1',
      thread_id: 'session-1',
      sequence: 1,
      role: 'assistant',
      content: '已读取文件',
      timestamp: '2026-07-14T00:00:00Z',
      toolCalls: [
        {
          callId: 'call-1',
          toolName: 'file_read',
          toolArgs: { file_path: 'src/main.ts' },
          status: 'completed',
          containerTaskId: 'task_abc123',
        },
      ],
    }

    const message = mapBackendMessageToMessage(
      backendMessage as any,
      'session-1',
    )

    const toolCallPart = message.parts?.find((p) => p.type === 'tool_call') as
      | ToolCallPart
      | undefined

    expect(toolCallPart).toBeDefined()
    // 回归断言：必须读到 camelCase 的 containerTaskId，不能是 undefined
    expect(toolCallPart!.containerTaskId).toBe('task_abc123')
  })

  it('后端未下发 containerTaskId 时应降级为 undefined（不报错）', () => {
    const backendMessage = {
      id: 'msg-2',
      thread_id: 'session-1',
      sequence: 2,
      role: 'assistant',
      content: '',
      timestamp: '2026-07-14T00:00:00Z',
      toolCalls: [
        {
          callId: 'call-2',
          toolName: 'bash',
          toolArgs: { command: 'ls' },
          status: 'completed',
          // 故意不传 containerTaskId
        },
      ],
    }

    const message = mapBackendMessageToMessage(
      backendMessage as any,
      'session-1',
    )

    const toolCallPart = message.parts?.find((p) => p.type === 'tool_call') as
      | ToolCallPart
      | undefined

    expect(toolCallPart).toBeDefined()
    expect(toolCallPart!.containerTaskId).toBeUndefined()
  })

  it('snake_case 的 container_task_id 字段不应被误读（防止反向回归）', () => {
    // 防御性断言：即便后端误传 snake_case，也不应被取到
    // （后端契约是 camelCase，这里确认映射函数不会意外兼容错误字段名）
    const backendMessage = {
      id: 'msg-3',
      thread_id: 'session-1',
      sequence: 3,
      role: 'assistant',
      content: '',
      timestamp: '2026-07-14T00:00:00Z',
      toolCalls: [
        {
          callId: 'call-3',
          toolName: 'file_write',
          toolArgs: { path: 'a.ts' },
          status: 'completed',
          containerTaskId: 'task_camel',
          // 旧 bug 读取的 snake_case 字段，不应生效
          container_task_id: 'task_snake_should_be_ignored',
        },
      ],
    }

    const message = mapBackendMessageToMessage(
      backendMessage as any,
      'session-1',
    )

    const toolCallPart = message.parts?.find((p) => p.type === 'tool_call') as
      | ToolCallPart
      | undefined

    expect(toolCallPart).toBeDefined()
    expect(toolCallPart!.containerTaskId).toBe('task_camel')
  })
})
