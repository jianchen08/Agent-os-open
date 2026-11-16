import { createServer, type Server } from 'node:http'
import { AddressInfo } from 'node:net'

/**
 * Mock sync_relay —— node:http 内存实现（端点契约按 sync_relay server.py）：
 * /enroll admin JWT 直签发 + per-device token 鉴权 + 设备注册表 + bootstrap 基线 +
 * cursor 增量拉取 + 心跳。错误体 {"error":{"code","message"}} 同构。
 */

export interface MockRelayEventBase {
  ulid: string
  device_id: string
  asset_type: string
  asset_id: string
  version: number
  updated_at: string
  payload: Record<string, unknown>
  hlc: { physical_ms: number; logical: number; device_id: string }
}

export type MockRelayEvent = MockRelayEventBase & { cursor: number }

export interface MockRelayHandle {
  url: string
  close: () => Promise<void>
  /** 追加账本事件（cursor 自动发号）。 */
  addEvent: (event: MockRelayEventBase) => void
  /** 设备注册表现照（device_id → {tier, heartbeats}）。 */
  devices: () => Map<string, { tier: string; heartbeats: number }>
  eventCount: () => number
}

export function startMockRelay(): Promise<MockRelayHandle> {
  return new Promise((resolveServer) => {
    const tokens = new Map<string, string>()
    const devices = new Map<string, { tier: string; heartbeats: number }>()
    const events: MockRelayEvent[] = []
    let nextCursor = 0
    let tokenSeq = 0

    const server: Server = createServer((req, res) => {
      const chunks: Buffer[] = []
      req.on('data', (chunk: Buffer) => chunks.push(chunk))
      req.on('end', () => {
        const body = JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}') as Record<string, unknown>
        const url = new URL(req.url ?? '/', 'http://mock')
        const respond = (status: number, payload: unknown) => {
          res.writeHead(status, { 'Content-Type': 'application/json' })
          res.end(JSON.stringify(payload))
        }
        const bearer = (req.headers.authorization ?? '').replace(/^Bearer /, '')
        const deviceOf = () => {
          for (const [device, token] of tokens) if (token === bearer) return device
          return null
        }
        const deny = () => respond(403, { error: { code: '403', message: 'unrecognized per-device token' } })

        if (url.pathname === '/ext/sync_relay/enroll' && req.method === 'POST') {
          const deviceId = String(body.device_id ?? '')
          if (!deviceId) return respond(400, { error: { code: '400', message: 'device_id 必须是非空字符串' } })
          if (!bearer || bearer === 'jwt-invalid') {
            return respond(401, { error: { code: '401', message: '入网凭据未通过核验（admin JWT）' } })
          }
          const token = `tok-${++tokenSeq}`
          tokens.set(deviceId, token) // 幂等换发：同设备重入网顶旧绑定
          devices.set(deviceId, {
            tier: String(body.tier ?? devices.get(deviceId)?.tier ?? ''),
            heartbeats: devices.get(deviceId)?.heartbeats ?? 0,
          })
          return respond(200, { device_id: deviceId, device_token: token, tenant_id: 'tenant-mock' })
        }

        if (url.pathname === '/ext/sync_relay/devices' && req.method === 'POST') {
          const device = deviceOf()
          if (!device) return deny()
          devices.set(device, { tier: String(body.tier ?? devices.get(device)?.tier ?? ''), heartbeats: devices.get(device)?.heartbeats ?? 0 })
          return respond(200, { registered: { device_id: device, tier: body.tier } })
        }

        if (url.pathname === '/ext/sync_relay/devices/heartbeat' && req.method === 'POST') {
          const device = deviceOf()
          if (!device) return deny()
          const entry = devices.get(device) ?? { tier: '', heartbeats: 0 }
          entry.heartbeats += 1
          devices.set(device, entry)
          return respond(200, { heartbeat_at: '2026-10-17T00:30:00.000Z' })
        }

        if (url.pathname === '/ext/sync_relay/bootstrap' && req.method === 'GET') {
          const device = deviceOf()
          if (!device) return deny()
          return respond(200, { device, events, cursor: nextCursor, tiers: {} })
        }

        if (url.pathname === '/ext/sync_relay/events' && req.method === 'GET') {
          const device = deviceOf()
          if (!device) return deny()
          const since = Number(url.searchParams.get('since_cursor') ?? '0')
          const page = events.filter((event) => event.cursor > since)
          return respond(200, { events: page, count: page.length, device })
        }

        respond(404, { error: { code: '404', message: `no route for ${req.method} ${url.pathname}` } })
      })
    })

    server.listen(0, '127.0.0.1', () => {
      const address = server.address() as AddressInfo
      const base = `http://127.0.0.1:${address.port}/ext/sync_relay`
      resolveServer({
        url: base,
        close: () => new Promise((done) => server.close(() => done())),
        addEvent: (event) => {
          events.push({ ...event, cursor: ++nextCursor })
        },
        devices: () => devices,
        eventCount: () => events.length,
      })
    })
  })
}
