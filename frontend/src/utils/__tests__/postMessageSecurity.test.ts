/**
 * postMessageSecurity 单测（Webview widget 安全边界）
 */
import { describe, it, expect } from 'vitest'
import {
  buildWebviewMessage,
  isTrustedWebviewOrigin,
  isWebviewMessage,
  validateWebviewEvent,
  SANDBOX_IFRAME_ORIGIN,
} from '@/utils/postMessageSecurity'

describe('postMessageSecurity', () => {
  it('sandbox iframe origin 固定为 "null"', () => {
    expect(SANDBOX_IFRAME_ORIGIN).toBe('null')
  })

  it('isTrustedWebviewOrigin 只接受 "null"', () => {
    expect(isTrustedWebviewOrigin('null')).toBe(true)
    expect(isTrustedWebviewOrigin('https://evil.com')).toBe(false)
    expect(isTrustedWebviewOrigin('http://localhost:5290')).toBe(false)
  })

  it('isWebviewMessage 校验魔数 + method', () => {
    expect(isWebviewMessage({ __agentos_webview: true, method: 'tool.invoke' })).toBe(true)
    expect(isWebviewMessage({ __agentos_webview: true, method: 'x', params: { a: 1 } })).toBe(true)
    // 缺魔数
    expect(isWebviewMessage({ method: 'tool.invoke' })).toBe(false)
    // 缺 method
    expect(isWebviewMessage({ __agentos_webview: true })).toBe(false)
    // method 非字符串
    expect(isWebviewMessage({ __agentos_webview: true, method: 123 })).toBe(false)
    // 空字符串 method
    expect(isWebviewMessage({ __agentos_webview: true, method: '' })).toBe(false)
    // 非 object
    expect(isWebviewMessage('hello')).toBe(false)
    expect(isWebviewMessage(null)).toBe(false)
  })

  it('buildWebviewMessage 构造合法消息', () => {
    const msg = buildWebviewMessage('widget.event', { v: 1 }, 'id_1')
    expect(msg.__agentos_webview).toBe(true)
    expect(msg.method).toBe('widget.event')
    expect(msg.params).toEqual({ v: 1 })
    expect(msg.id).toBe('id_1')
  })

  it('buildWebviewMessage params/id 可选', () => {
    const msg = buildWebviewMessage('ready')
    expect(msg.params).toBeUndefined()
    expect(msg.id).toBeUndefined()
  })

  it('validateWebviewEvent 拒绝不可信 origin', () => {
    const event = {
      origin: 'https://evil.com',
      data: { __agentos_webview: true, __wv_token: 'tok', method: 'x' },
    } as MessageEvent
    expect(validateWebviewEvent(event, 'tok')).toBeNull()
  })

  it('validateWebviewEvent 拒绝不符合协议的消息', () => {
    const event = {
      origin: 'null',
      data: { method: 'x' }, // 缺魔数
    } as MessageEvent
    expect(validateWebviewEvent(event, 'tok')).toBeNull()
  })

  it('validateWebviewEvent 拒绝缺令牌 / 令牌不匹配', () => {
    const noToken = {
      origin: 'null',
      data: { __agentos_webview: true, method: 'x' },
    } as MessageEvent
    expect(validateWebviewEvent(noToken, 'tok')).toBeNull()

    const wrongToken = {
      origin: 'null',
      data: { __agentos_webview: true, __wv_token: 'forged', method: 'x' },
    } as MessageEvent
    expect(validateWebviewEvent(wrongToken, 'tok')).toBeNull()
  })

  it('validateWebviewEvent 通过合法消息（origin + 魔数 + 令牌全匹配）', () => {
    const event = {
      origin: 'null',
      data: { __agentos_webview: true, __wv_token: 'tok_123', method: 'tool.invoke', params: { id: 't1' } },
    } as MessageEvent
    const result = validateWebviewEvent(event, 'tok_123')
    expect(result).not.toBeNull()
    expect(result?.method).toBe('tool.invoke')
    expect(result?.params).toEqual({ id: 't1' })
  })
})
