/** @feature FP-0.2.多端同步 远端内核实时通道中继 | @ci: frontend-test */
/**
 * wsRelay 单测：远端内核 WS 中继 URL 解析契约（桌面版，2026-10-22）
 *
 * 断行为：
 * - 无宿主能力（浏览器/dev/移动壳）或空基址 → null，调用方回落直连
 * - 有宿主能力：先 allow(目标基址) 声明唯一转发目标，再取中继 URL（携带
 *   协议版本；last_sequence 仅在有有效续传位点 >0 时下发）
 * - 中继未授权/主进程未起（allow/url 抛错）→ null（直连兜底，不抛错冒泡）
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import { PROTOCOL_VERSION } from '@/constants/websocket'
import { resolveRelayWsUrl } from '../wsRelay'

const BASE = 'http://203.0.113.153:9101'
const RELAY_URL = 'ws://127.0.0.1:39101/ws/chat'

interface WsRelayStub {
  allow: (target: string | null) => Promise<boolean>
  url: (params: {
    target: string
    ticket: string
    version?: string
    last_sequence?: number
  }) => Promise<string | null>
}

function stubRelay(relay: WsRelayStub | undefined): void {
  ;(window as unknown as { electronAPI?: { wsRelay?: WsRelayStub } }).electronAPI =
    relay ? { wsRelay: relay } : undefined
}

function makeRelay(overrides: Partial<WsRelayStub> = {}): {
  relay: WsRelayStub
  allow: ReturnType<typeof vi.fn>
  url: ReturnType<typeof vi.fn>
} {
  const allow = vi.fn(async () => true)
  const url = vi.fn(async () => RELAY_URL as string | null)
  const relay: WsRelayStub = {
    allow: overrides.allow ?? allow,
    url: overrides.url ?? url,
  }
  return { relay, allow, url }
}

afterEach(() => {
  stubRelay(undefined)
})

describe('resolveRelayWsUrl', () => {
  it('无 electronAPI（浏览器/dev/移动壳）→ null，回落直连', async () => {
    stubRelay(undefined)
    expect(await resolveRelayWsUrl(BASE, 'ticket-1')).toBeNull()
  })

  it('空基址 → null 且不触碰宿主能力（未声明目标不发中继请求）', async () => {
    const { relay, allow, url } = makeRelay()
    stubRelay(relay)

    expect(await resolveRelayWsUrl('', 'ticket-1')).toBeNull()
    expect(allow).not.toHaveBeenCalled()
    expect(url).not.toHaveBeenCalled()
  })

  it('有宿主能力：先 allow 声明目标基址，再取中继 URL（带协议版本）', async () => {
    const { relay, allow, url } = makeRelay()
    stubRelay(relay)

    expect(await resolveRelayWsUrl(BASE, 'ticket-1')).toBe(RELAY_URL)
    expect(allow).toHaveBeenCalledWith(BASE)
    expect(url).toHaveBeenCalledWith({
      target: BASE,
      ticket: 'ticket-1',
      version: PROTOCOL_VERSION,
    })
    // allow 先于 url（未声明目标的取 URL 会被主进程拒）
    expect(allow.mock.invocationCallOrder[0]).toBeLessThan(url.mock.invocationCallOrder[0])
  })

  it('lastSequence 有效（>0）→ 下发续传位点', async () => {
    const { relay, url } = makeRelay()
    stubRelay(relay)

    expect(await resolveRelayWsUrl(BASE, 'ticket-2', 12)).toBe(RELAY_URL)
    expect(url).toHaveBeenCalledWith({
      target: BASE,
      ticket: 'ticket-2',
      version: PROTOCOL_VERSION,
      last_sequence: 12,
    })
  })

  it('lastSequence=0 → 不下发（0 不是有效续传位点）', async () => {
    const { relay, url } = makeRelay()
    stubRelay(relay)

    await resolveRelayWsUrl(BASE, 'ticket-3', 0)
    expect(url).toHaveBeenCalledWith({
      target: BASE,
      ticket: 'ticket-3',
      version: PROTOCOL_VERSION,
    })
  })

  it('中继未授权（allow 抛错）→ null，不取 URL', async () => {
    const { relay, url } = makeRelay({
      allow: vi.fn(async () => {
        throw new Error('relay not ready')
      }),
    })
    stubRelay(relay)

    expect(await resolveRelayWsUrl(BASE, 'ticket-4')).toBeNull()
    expect(url).not.toHaveBeenCalled()
  })

  it('取 URL 抛错（主进程未起）→ null（直连兜底）', async () => {
    const { relay } = makeRelay({
      url: vi.fn(async () => {
        throw new Error('relay crashed')
      }),
    })
    stubRelay(relay)

    expect(await resolveRelayWsUrl(BASE, 'ticket-5')).toBeNull()
  })

  it('主进程拒绝出 URL（返回 null）→ null 原样透传', async () => {
    const { relay } = makeRelay({ url: vi.fn(async () => null) })
    stubRelay(relay)

    expect(await resolveRelayWsUrl(BASE, 'ticket-6')).toBeNull()
  })
})
