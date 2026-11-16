/**
 * 集成路径 —— 真 RelayClient + 真 HTTP（node:http Mock relay，端点契约同构
 * sync_relay）：enroll（登录直签发）→ register → bootstrap 基线应用 → 增量拉取
 * → 心跳 → unpair 全链走真实依赖（关键路径零 mock 传输层）。
 */
// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { CredentialStore } from '../credential'
import { ConfigStore } from '../configStore'
import { RelayClient, RelayError } from '../relayClient'
import { SyncEngine } from '../syncEngine'
import { memoryKv } from './helpers'
import { startMockRelay, type MockRelayHandle } from './mockRelay'
import { newUlid } from '../ulid'

const T0 = 1_760_000_000_000

describe('enroll → bootstrap → pull → heartbeat 全链（真实 HTTP）', () => {
  let relay: MockRelayHandle

  beforeEach(async () => {
    relay = await startMockRelay()
  })

  afterEach(async () => {
    await relay.close()
  })

  function makeEngine(): { engine: SyncEngine; store: ConfigStore } {
    const { kv } = memoryKv()
    const store = new ConfigStore(kv)
    const engine = new SyncEngine({
      credential: new CredentialStore(kv, null),
      store,
      client: new RelayClient(),
      joinRelayUrl: () => relay.url,
      schedule: () => () => undefined,
      clockMs: () => T0,
      appVersion: '0.2.2-test',
    })
    return { engine, store }
  }

  function desktopEvent(content: string, physicalMs: number, assetId = 'agents/main/agentos.yaml') {
    return {
      ulid: newUlid(physicalMs),
      device_id: 'DESKTOP-41ST1LM',
      asset_type: 'config',
      asset_id: assetId,
      version: 1,
      updated_at: '2026-10-17T00:00:00.000Z',
      payload: { content },
      hlc: { physical_ms: physicalMs, logical: 0, device_id: 'DESKTOP-41ST1LM' },
    }
  }

  it('登录入网后基线与增量收敛、心跳在链、解除配对退出', async () => {
    relay.addEvent(desktopEvent('agents_boot: v1\n', T0))
    const { engine, store } = makeEngine()

    // enroll：真 HTTP 直签发，tier=phone 设备进注册表
    await engine.joinWithAuth('jwt-admin')
    const registry = [...relay.devices().entries()]
    expect(registry).toHaveLength(1)
    const [deviceId, entry] = registry[0]!
    expect(entry.tier).toBe('phone')
    expect(deviceId).toMatch(/^[\da-f-]{36}$/)

    // 自举：基线事件应用 + 游标定位账本头
    await engine.syncNow()
    expect(store.loadConfigContent('agents/main/agentos.yaml')).toBe('agents_boot: v1\n')
    expect(store.loadCursor()).toBe(1)
    expect(relay.devices().get(deviceId)?.heartbeats).toBeGreaterThanOrEqual(1)

    // 桌面新资产事件（更高 HLC）→ 拉取应用；旧值事件对已应用资产 → stale 拒收
    relay.addEvent(desktopEvent('pipelines_boot: v2\n', T0 + 1_000, 'pipelines/autonomous.yaml'))
    relay.addEvent(desktopEvent('stale_line: old\n', T0 - 5_000))
    await engine.syncNow()
    expect(store.loadConfigContent('pipelines/autonomous.yaml')).toBe('pipelines_boot: v2\n')
    expect(store.loadConfigContent('agents/main/agentos.yaml')).toBe('agents_boot: v1\n')
    expect(engine.status().staleTotal).toBe(1)
    expect(store.loadCursor()).toBe(3)

    // 重复入网幂等换发：同设备重 enroll 顶旧 token，注册表不增长
    await engine.joinWithAuth('jwt-admin')
    expect(relay.devices().size).toBe(1)
    expect(engine.status().paired).toBe(true)

    await engine.unpair()
    expect(engine.status().paired).toBe(false)
  })

  it('入网凭据无效：relay 401 原样上抛、设备不进注册表', async () => {
    const { engine } = makeEngine()
    await expect(engine.joinWithAuth('jwt-invalid')).rejects.toThrow('入网凭据未通过核验')
    expect(relay.devices().size).toBe(0)
  })

  it('空 JWT：端侧 400 拒绝且不发请求', async () => {
    const { engine } = makeEngine()
    await expect(engine.joinWithAuth('   ')).rejects.toThrow(RelayError)
    expect(relay.devices().size).toBe(0)
  })

  it('token 鉴权面：错 token 访问设备面端点 403（fail-closed 复验）', async () => {
    const client = new RelayClient()
    await expect(client.pullEvents(relay.url, 'forged-token', 0)).rejects.toMatchObject({ status: 403 })
  })
})
