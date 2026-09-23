/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * deriveWsUrl 测试：WebSocket 基址派生
 *
 * 覆盖四类输入：
 * - 显式 apiUrl（http/https）→ 协议升级为 ws/wss
 * - 空 apiUrl + http(s) 页面源 → 从 location 派生（dev Vite 代理 / nginx 同源）
 * - 空 apiUrl + app: 页面源（BUG-9 打包件自定义协议）→ 内核直连 ws://127.0.0.1:9101
 *   （app:// 页面无法从 location 派生 ws 地址：host 是自定义协议宿主而非内核）
 */

import { describe, expect, it } from 'vitest'
import { deriveWsUrl } from '@/constants/websocket'

/** 构造最小 location 形状（deriveWsUrl 只消费 protocol/host） */
function fakeLoc(protocol: string, host: string): Location {
  return { protocol, host } as unknown as Location
}

describe('deriveWsUrl', () => {
  it('显式 http apiUrl → ws 同源升级', () => {
    expect(deriveWsUrl('http://127.0.0.1:9100')).toBe('ws://127.0.0.1:9100')
  })

  it('显式 https apiUrl → wss', () => {
    expect(deriveWsUrl('https://api.example.com')).toBe('wss://api.example.com')
  })

  it('空 apiUrl + http 页面（dev Vite 代理）→ 从 location 派生 ws://host', () => {
    expect(deriveWsUrl('', fakeLoc('http:', 'localhost:5188'))).toBe('ws://localhost:5188')
  })

  it('空 apiUrl + https 页面（nginx 同源）→ wss://host', () => {
    expect(deriveWsUrl('', fakeLoc('https:', 'agentos.example.com'))).toBe(
      'wss://agentos.example.com',
    )
  })

  it('空 apiUrl + app: 页面（打包件）→ 内核直连 ws://127.0.0.1:9101（装机版默认端口，与 dev 9100 错峰）', () => {
    expect(deriveWsUrl('', fakeLoc('app:', 'bundle'))).toBe('ws://127.0.0.1:9101')
  })

  it('性质：任意输入结果恒为合法 ws/wss URL（非空且带可解析 origin）', () => {
    const cases: Array<[string, Location]> = [
      ['http://127.0.0.1:9100', fakeLoc('https:', 'x')],
      ['', fakeLoc('http:', 'localhost:5188')],
      ['', fakeLoc('app:', 'bundle')],
    ]
    for (const [apiUrl, loc] of cases) {
      const url = deriveWsUrl(apiUrl, loc)
      expect(url.startsWith('ws://') || url.startsWith('wss://')).toBe(true)
      expect(() => new URL(url)).not.toThrow()
      expect(new URL(url).host).not.toBe('')
    }
  })
})
