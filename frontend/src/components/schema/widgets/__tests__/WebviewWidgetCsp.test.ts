/** @feature FP-0.2.四 WebviewWidget CSP 白名单扩展 | @ci: frontend-test */
/**
 * WebviewWidget CSP 白名单扩展测试
 *
 * 契约（ADR 2026-10-21-browser-live-workspace-tab，用户裁定：扩展机制+可配置白名单）：
 * - 默认（无扩展）与 CSP_META 逐字节一致——存量 content_cards/插件页零变化
 * - img-src 扩展按声明合并（browser_tool 直播页 img → Bridge origin）
 * - fail-closed：非法条目逐条丢弃（协议非 http(s)/缺 host/带路径/bare '*'）
 * - 高危向锁死：扩展声明 script-src/connect-src 不存在落点，meta 恒 connect-src 'none'
 */
import { describe, expect, it } from 'vitest'
import { CSP_META, buildCspMeta, normalizeCspExtension } from '../WebviewWidget'

describe('buildCspMeta — 默认基线', () => {
  it('无扩展 = CSP_META 逐字节一致', () => {
    expect(buildCspMeta()).toBe(CSP_META)
    expect(buildCspMeta(null)).toBe(CSP_META)
    expect(buildCspMeta(normalizeCspExtension(undefined))).toBe(CSP_META)
  })
})

describe('normalizeCspExtension — fail-closed 归一', () => {
  it('合法 origin 收录并去重', () => {
    const ext = normalizeCspExtension({
      imgSrc: ['http://127.0.0.1:*', 'http://localhost:*', 'http://127.0.0.1:*', 'https://cdn.example.com:8443'],
    })
    expect(ext).toEqual({ imgSrc: ['http://127.0.0.1:*', 'http://localhost:*', 'https://cdn.example.com:8443'] })
  })

  it.each([
    ['bare *', '*'],
    ['缺协议', '127.0.0.1:8765'],
    ['带路径', 'http://127.0.0.1:8765/x'],
    ['非 http 协议', 'javascript:alert(1)'],
    ['unsafe 关键字', "'unsafe-inline'"],
    ['空串', ''],
  ])('非法条目丢弃：%s', (_name, bad) => {
    expect(normalizeCspExtension({ imgSrc: [bad, 'http://127.0.0.1:*'] })).toEqual({
      imgSrc: ['http://127.0.0.1:*'],
    })
  })

  it.each([
    ['非数组 imgSrc', { imgSrc: 'http://127.0.0.1:*' }],
    ['raw 非对象', 'http://127.0.0.1:*'],
    ['全非法条目', { imgSrc: ['*', 'x'] }],
  ])('整包丢弃 → null：%s', (_name, raw) => {
    expect(normalizeCspExtension(raw)).toBeNull()
  })
})

describe('buildCspMeta — 扩展合成', () => {
  it('imgSrc 追加进 img-src 指令', () => {
    const meta = buildCspMeta({ imgSrc: ['http://127.0.0.1:*', 'http://localhost:*'] })
    expect(meta).toContain("img-src data: http://127.0.0.1:* http://localhost:*;")
    expect(meta).toContain("connect-src 'none'")
    expect(meta).toContain("script-src 'unsafe-inline'")
  })
})
