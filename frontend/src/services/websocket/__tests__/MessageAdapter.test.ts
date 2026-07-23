/**
 * WebSocket 兼容层测试
 *
 * 覆盖 AC-11-4: WebSocket 消息格式与 Rust 内核兼容，前端消息收发正常
 * 覆盖 AC-11-5: 向后兼容（0.1 消息格式继续可用）
 */

import { describe, it, expect, beforeEach } from 'vitest'
import {
  adaptIncomingMessage,
  adaptOutgoingMessage,
  isRustKernelMessage,
  adaptWidgetEvent,
  isWidgetEvent,
} from '@/services/websocket/MessageAdapter'
import type { RawWSMessage } from '@/services/websocket/MessageAdapter'

describe('MessageAdapter — AC-11-4: Rust 内核消息格式适配', () => {
  describe('adaptIncomingMessage', () => {
    it('Rust 内核消息（扁平 type + data）正确适配为前端格式', () => {
      const raw: RawWSMessage = {
        type: 'pipeline_chunk',
        data: {
          thread_id: 't-123',
          content: 'Hello',
          seq: 1,
        },
      }
      const adapted = adaptIncomingMessage(raw)

      expect(adapted.type).toBe('pipeline_chunk')
      expect(adapted.thread_id).toBe('t-123')
      expect(adapted.data).toEqual({ thread_id: 't-123', content: 'Hello', seq: 1 })
    })

    it('Rust 内核消息 metadata 字段正确透传', () => {
      const raw: RawWSMessage = {
        type: 'pipeline_complete',
        data: { thread_id: 't-456' },
        metadata: { pipeline_id: 'p-789', duration_ms: 1500 },
      }
      const adapted = adaptIncomingMessage(raw)

      expect(adapted.metadata).toEqual({ pipeline_id: 'p-789', duration_ms: 1500 })
    })

    it('无 type 字段的消息返回 null', () => {
      const raw: RawWSMessage = { data: { foo: 'bar' } } as any
      const adapted = adaptIncomingMessage(raw)

      expect(adapted).toBeNull()
    })

    it('无 data 字段的消息仍可适配（data 可选）', () => {
      const raw: RawWSMessage = { type: 'heartbeat_ack' }
      const adapted = adaptIncomingMessage(raw)

      expect(adapted).not.toBeNull()
      expect(adapted?.type).toBe('heartbeat_ack')
    })
  })

  describe('adaptOutgoingMessage', () => {
    it('前端格式适配为 Rust 内核格式（type + data + metadata）', () => {
      const outgoing = {
        type: 'user_input',
        thread_id: 't-123',
        content: 'Hello',
        pipeline_id: 'p-456',
      }
      const adapted = adaptOutgoingMessage(outgoing)

      expect(adapted.type).toBe('user_input')
      expect(adapted.data).toBeDefined()
      expect(adapted.data.thread_id).toBe('t-123')
      expect(adapted.data.content).toBe('Hello')
      expect(adapted.data.pipeline_id).toBe('p-456')
    })

    it('保留可选的 metadata 字段', () => {
      const outgoing = {
        type: 'user_input',
        thread_id: 't-123',
        content: 'Hello',
        metadata: { client_version: '0.2.0' },
      }
      const adapted = adaptOutgoingMessage(outgoing)

      expect(adapted.metadata).toEqual({ client_version: '0.2.0' })
    })

    it('心跳消息适配保持简洁', () => {
      const outgoing = { type: 'heartbeat', timestamp: 1234567890 }
      const adapted = adaptOutgoingMessage(outgoing)

      expect(adapted.type).toBe('heartbeat')
      expect(adapted.data.timestamp).toBe(1234567890)
    })
  })

  describe('isRustKernelMessage', () => {
    it('含 data 字段的扁平消息识别为 Rust 内核格式', () => {
      expect(isRustKernelMessage({ type: 'x', data: {} })).toBe(true)
    })

    it('不含 data 字段的消息识别为非 Rust 内核格式（可能是 0.1 Python 格式）', () => {
      expect(isRustKernelMessage({ type: 'x', thread_id: 't-1' })).toBe(false)
    })

    it('空对象识别为非 Rust 内核格式', () => {
      expect(isRustKernelMessage({})).toBe(false)
    })
  })
})

describe('MessageAdapter — AC-11-5: 向后兼容', () => {
  it('0.1 Python 格式消息（扁平字段，无 data 包装）仍可适配', () => {
    const legacy: RawWSMessage = {
      type: 'pipeline_chunk',
      thread_id: 't-legacy',
      content: 'Legacy message',
      pipeline_id: 'p-1',
    }
    const adapted = adaptIncomingMessage(legacy)

    expect(adapted).not.toBeNull()
    expect(adapted?.type).toBe('pipeline_chunk')
    expect(adapted?.thread_id).toBe('t-legacy')
  })

  it('0.1 消息中 thread_id 等顶层字段正确提取', () => {
    const legacy: RawWSMessage = {
      type: 'pipeline_start',
      thread_id: 't-old',
      pipeline_id: 'p-old',
    }
    const adapted = adaptIncomingMessage(legacy)

    expect(adapted?.thread_id).toBe('t-old')
    expect(adapted?.data?.pipeline_id).toBe('p-old')
  })
})

describe('MessageAdapter — widget_event 族解析（P2 协议）', () => {
  // P2 后端将下发：{ type: 'widget_event', data: { widget_id, event, data }, sequence }
  // 数据 data 包装层是 Rust 0.2 格式，但顶层多了一个 sequence 字段需要保留。

  it('isWidgetEvent 识别 widget_event 类型消息', () => {
    expect(isWidgetEvent({ type: 'widget_event', data: {}, sequence: 1 })).toBe(true)
    expect(isWidgetEvent({ type: 'pipeline_chunk', data: {} })).toBe(false)
    expect(isWidgetEvent({})).toBe(false)
  })

  it('adaptWidgetEvent 解析 widget_id / event / payload / sequence', () => {
    const raw: RawWSMessage = {
      type: 'widget_event',
      data: {
        widget_id: 'review_panel',
        event: 'doc_loaded',
        data: { doc_id: 'd-1', pages: 3 },
      },
      sequence: 42,
    }

    const adapted = adaptWidgetEvent(raw)

    expect(adapted).not.toBeNull()
    expect(adapted?.widget_id).toBe('review_panel')
    expect(adapted?.event).toBe('doc_loaded')
    expect(adapted?.data).toEqual({ doc_id: 'd-1', pages: 3 })
    expect(adapted?.sequence).toBe(42)
  })

  it('adaptWidgetEvent 对非 widget_event 消息返回 null', () => {
    expect(adaptWidgetEvent({ type: 'pipeline_chunk', data: {} })).toBeNull()
    expect(adaptWidgetEvent({})).toBeNull()
  })

  it('adaptWidgetEvent 缺失 sequence 时 sequence 为 undefined', () => {
    const adapted = adaptWidgetEvent({
      type: 'widget_event',
      data: { widget_id: 'w', event: 'e', data: {} },
    })
    expect(adapted?.widget_id).toBe('w')
    expect(adapted?.sequence).toBeUndefined()
  })

  it('adaptWidgetEvent 缺失 data 包装时返回 null（防御无效格式）', () => {
    expect(
      adaptWidgetEvent({ type: 'widget_event' }),
    ).toBeNull()
  })

  it('adaptWidgetEvent 缺失 widget_id/event 时相应字段为 undefined（不抛错）', () => {
    const adapted = adaptWidgetEvent({
      type: 'widget_event',
      data: { data: { x: 1 } },
      sequence: 5,
    })
    expect(adapted?.widget_id).toBeUndefined()
    expect(adapted?.event).toBeUndefined()
    expect(adapted?.data).toEqual({ x: 1 })
    expect(adapted?.sequence).toBe(5)
  })

  it('adaptIncomingMessage 也能透传 widget_event（兼容通用入口）', () => {
    const raw: RawWSMessage = {
      type: 'widget_event',
      data: { widget_id: 'w', event: 'e', data: { k: 'v' } },
      sequence: 7,
    }
    const adapted = adaptIncomingMessage(raw)
    expect(adapted?.type).toBe('widget_event')
    expect(adapted?.data.widget_id).toBe('w')
  })
})
