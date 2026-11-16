import { beforeEach, describe, expect, it } from 'vitest'
import { ConfigStore } from '../configStore'
import { compareHlc } from '../hlc'
import { RelayError } from '../relayClient'
import { newUlid } from '../ulid'
import {
  JOIN_RELAY_URL,
  PHONE_DEVICE_ID,
  RELAY_URL,
  makeEngine,
  memoryKv,
  stagedEnroll,
  type EngineFixture,
} from './helpers'

const T0 = 1_760_000_000_000
const ASSET_ID = 'agents/main/agentos.yaml'
const ASSET_KEY = `config/${ASSET_ID}`

interface WireOptions {
  ulid?: string
  deviceId?: string
  hlc?: { physicalMs: number; logical: number; deviceId: string } | null
  content?: unknown
  assetId?: string
  assetType?: string
  cursor?: number
}

/** 下行 wire 构造器（合法缺省，逐用例覆写）。 */
function wire(options: WireOptions = {}): Record<string, unknown> {
  const hlc = options.hlc === undefined ? { physicalMs: T0, logical: 0, deviceId: 'desktop-1' } : options.hlc
  const payload: Record<string, unknown> = {}
  if (options.content !== null) payload.content = options.content ?? 'key: value\n'
  return {
    ulid: options.ulid ?? newUlid(T0),
    device_id: options.deviceId ?? 'desktop-1',
    asset_type: options.assetType ?? 'config',
    asset_id: options.assetId ?? ASSET_ID,
    version: 1,
    updated_at: '2026-10-17T00:00:00.000Z',
    payload,
    hlc:
      hlc === null
        ? null
        : { physical_ms: hlc.physicalMs, logical: hlc.logical, device_id: hlc.deviceId },
    cursor: options.cursor ?? 0,
  }
}

async function seedLocalAsset(store: ConfigStore, hlc: { physicalMs: number; logical: number; deviceId: string }): Promise<void> {
  store.saveConfigContent(ASSET_ID, 'local: current\n')
  store.saveAsset(ASSET_KEY, {
    hlc,
    version: 1,
    ulid: newUlid(T0),
    contentHash: 'seed-hash',
  })
}

describe('HLC-LWW 合并：后改赢（顺序双向参数化）', () => {
  let fx: EngineFixture
  beforeEach(() => {
    fx = makeEngine()
    stagedEnroll(fx.relay)
    return fx.engine.joinWithAuth('jwt-admin').then(() => undefined)
  })

  it('先旧后新：云端旧值先到、手机新值后到 → 后到者应用（新事件胜）', async () => {
    const oldEvent = wire({ hlc: { physicalMs: T0, logical: 0, deviceId: 'desktop-1' }, content: 'v: old\n' })
    const newEvent = wire({ hlc: { physicalMs: T0 + 500, logical: 0, deviceId: 'phone-x' }, content: 'v: new\n' })
    expect(await fx.engine.ingestWire(oldEvent)).toBe('applied')
    expect(await fx.engine.ingestWire(newEvent)).toBe('applied')
    expect(fx.store.loadConfigContent(ASSET_ID)).toBe('v: new\n')
  })

  it('手机反例：手机后改的高 HLC 值在账，云端旧值后到 → stale 拒收，手机值不被覆盖', async () => {
    await seedLocalAsset(fx.store, { physicalMs: T0 + 500, logical: 0, deviceId: 'phone-x' })
    const staleCloud = wire({ hlc: { physicalMs: T0, logical: 0, deviceId: 'desktop-1' }, content: 'v: stale-cloud\n' })
    expect(await fx.engine.ingestWire(staleCloud)).toBe('stale')
    expect(fx.store.loadConfigContent(ASSET_ID)).toBe('local: current\n')
    expect(fx.engine.status().staleTotal).toBe(1)
  })

  it('同物理同逻辑毫秒：device_id 字典序决胜，恒有唯一胜者', async () => {
    await seedLocalAsset(fx.store, { physicalMs: T0, logical: 0, deviceId: 'phone-x' })
    // cloud < phone-x 字典序：同毫秒并发冲突时本端现值胜（裁决结果与到达序无关）。
    const losing = wire({ hlc: { physicalMs: T0, logical: 0, deviceId: 'cloud' }, content: 'v: losing\n' })
    expect(await fx.engine.ingestWire(losing)).toBe('stale')
    const winning = wire({ hlc: { physicalMs: T0, logical: 0, deviceId: 'zzz-device' }, content: 'v: winning\n' })
    expect(await fx.engine.ingestWire(winning)).toBe('applied')
    expect(fx.store.loadConfigContent(ASSET_ID)).toBe('v: winning\n')
    expect(compareHlc(fx.store.assetHlc(ASSET_KEY)!, { physicalMs: T0, logical: 0, deviceId: 'zzz-device' })).toBe(0)
  })

  it('应用后账本面齐备：HLC/版本/ULID/SHA-256 内容哈希', async () => {
    const content = 'key: value\n'
    const event = wire({ content })
    await fx.engine.ingestWire(event)
    const record = fx.store.loadAsset(ASSET_KEY)
    expect(record?.hlc).toEqual({ physicalMs: T0, logical: 0, deviceId: 'desktop-1' })
    expect(record?.version).toBe(1)
    expect(record?.ulid).toBe(event.ulid)
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(content))
    const hex = [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('')
    expect(record?.contentHash).toBe(hex)
  })
})

describe('合并拒绝面（fail-closed 语义，参数化）', () => {
  let fx: EngineFixture
  beforeEach(() => {
    fx = makeEngine()
    stagedEnroll(fx.relay)
    return fx.engine.joinWithAuth('jwt-admin').then(() => undefined)
  })

  it.each([
    ['五必填违约', wire({ ulid: 'BAD' }), 'invalid'],
    ['无 HLC（裸墙钟被否，端侧兜底拒收）', wire({ hlc: null, content: 'x' }), 'no_hlc'],
    ['payload 无 content', wire({ content: null }), 'no_content'],
    ['无 asset_id', wire({ assetId: '' }), 'no_asset_id'],
    ['非 config 资产面', wire({ assetType: 'session' }), 'unsupported_type'],
  ])('%s', async (_label, event, expected) => {
    expect(await fx.engine.ingestWire(event)).toBe(expected)
    expect(fx.store.loadConfigContent(ASSET_ID)).toBeNull()
  })

  it('本端事件回显跳过（不写账本不计数 stale）', async () => {
    const selfEvent = wire({ deviceId: PHONE_DEVICE_ID })
    expect(await fx.engine.ingestWire(selfEvent)).toBe('self')
    expect(fx.store.loadAsset(ASSET_KEY)).toBeNull()
  })

  it('ULID 幂等：同事件重放 duplicate，内容只应用一次', async () => {
    const event = wire({ ulid: '01ARZ3NDEKTSV4RRFFQ69G5FAV' })
    expect(await fx.engine.ingestWire(event)).toBe('applied')
    expect(await fx.engine.ingestWire(event)).toBe('duplicate')
    expect(fx.engine.status().appliedTotal).toBe(1)
    expect(fx.store.appliedCount()).toBe(1)
  })
})

describe('登录入网状态机', () => {
  it('joinWithAuth 成功：直签参数齐备（tier=phone）→ 凭据落盘 → 旧配对状态作废 → HLC 与设备 UUID 保留 → 起轮 30s', async () => {
    const fx = makeEngine()
    // 预置旧配对残留 + HLC 状态 + 设备 UUID（重入网场景）。
    fx.store.saveCursor(42)
    fx.store.saveConfigContent(ASSET_ID, 'stale\n')
    fx.store.saveAsset(ASSET_KEY, { hlc: { physicalMs: 1, logical: 0, deviceId: 'd' }, version: 1, ulid: newUlid(1), contentHash: 'h' })
    fx.store.saveHlcState(T0, 5)
    fx.store.saveDeviceId(PHONE_DEVICE_ID)
    stagedEnroll(fx.relay)
    fx.relay.enqueueBootstrap({ device: PHONE_DEVICE_ID, events: [], cursor: 9 })

    const active = await fx.engine.joinWithAuth('jwt-admin')

    // 直签请求契约：JWT 整串透传 + tier=phone + 设备 UUID + 走装配基址。
    expect(fx.relay.enrollCalls).toEqual([
      { relayUrl: JOIN_RELAY_URL, jwt: 'jwt-admin', deviceId: PHONE_DEVICE_ID, tier: 'phone' },
    ])
    expect(active.relayUrl).toBe(RELAY_URL)
    // 凭据持久化面（fixture 的 CredentialStore 同存储面现读）：
    const reread = await fx.credential.load()
    expect(reread).toMatchObject({ deviceId: PHONE_DEVICE_ID, deviceToken: 'token-phone-1', relayUrl: RELAY_URL, tier: 'phone' })
    // 旧配对状态作废：
    expect(fx.store.loadCursor()).toBe(9) // 自举即定位新账本头
    expect(fx.store.loadConfigContent(ASSET_ID)).toBeNull()
    expect(fx.store.loadAsset(ASSET_KEY)).toBeNull()
    // HLC 时钟与设备 UUID 保留：
    expect(fx.store.loadHlcState()).toEqual([T0, 5])
    expect(fx.store.loadDeviceId()).toBe(PHONE_DEVICE_ID)
    // 起轮 30s + 立即自举：
    expect(fx.scheduleCalls).toEqual([{ ms: 30_000 }])
    expect(fx.relay.bootstrapCalls).toBe(1)
    expect(fx.engine.status().paired).toBe(true)
  })

  it('joinWithAuth 显式基址覆盖：前端按当前连接基址入网（覆盖装配解析链）', async () => {
    const fx = makeEngine()
    stagedEnroll(fx.relay)
    await fx.engine.joinWithAuth('jwt-admin', 'http://hub.example:9300/ext/sync_relay')
    expect(fx.relay.enrollCalls).toEqual([
      {
        relayUrl: 'http://hub.example:9300/ext/sync_relay',
        jwt: 'jwt-admin',
        deviceId: PHONE_DEVICE_ID,
        tier: 'phone',
      },
    ])
  })

  it('joinWithAuth 失败（401）：错误上抛、零副作用（凭据/旧状态/调度全不动）', async () => {
    const fx = makeEngine()
    fx.store.saveCursor(42)
    fx.relay.rejectEnroll(new RelayError(401, '入网凭据未通过核验'))
    await expect(fx.engine.joinWithAuth('jwt-bad')).rejects.toThrow('入网凭据未通过核验')
    expect(fx.store.loadCursor()).toBe(42)
    expect(fx.scheduleCalls).toEqual([])
    expect(fx.engine.status().paired).toBe(false)
    expect(await fx.credential.load()).toBeNull()
  })

  it('空 JWT：400 拒绝且不发请求', async () => {
    const fx = makeEngine()
    await expect(fx.engine.joinWithAuth('   ')).rejects.toThrow('入网凭据为空')
    expect(fx.relay.enrollCalls).toEqual([])
  })

  it('unpair：凭据与配对状态同清，HLC 与设备 UUID 保留，调度取消，后续同步拒 400', async () => {
    const fx = makeEngine()
    fx.store.saveDeviceId(PHONE_DEVICE_ID)
    stagedEnroll(fx.relay)
    fx.relay.enqueueBootstrap({ device: PHONE_DEVICE_ID, events: [], cursor: 7 })
    await fx.engine.joinWithAuth('jwt-admin')
    fx.store.saveHlcState(T0, 3)

    await fx.engine.unpair()

    expect(await fx.credential.load()).toBeNull()
    expect(fx.store.loadCursor()).toBeNull()
    expect(fx.store.loadHlcState()).toEqual([T0, 3])
    expect(fx.store.loadDeviceId()).toBe(PHONE_DEVICE_ID)
    expect(fx.cancelled).toEqual([true])
    await expect(fx.engine.syncNow()).rejects.toThrow('尚未配对')
  })
})

describe('自举与拉取水位', () => {
  it('start：已配对且无游标 → 自举（注册+基线应用+游标定位账本头）', async () => {
    const fx = makeEngine()
    fx.store.saveDeviceId(PHONE_DEVICE_ID)
    await fx.credential.save({
      relayUrl: RELAY_URL,
      deviceId: PHONE_DEVICE_ID,
      deviceToken: 'token-1',
      tenantId: '',
      tier: 'phone',
      appVersion: '0.2.2-test',
      pairedAt: '2026-10-17T00:00:00.000Z',
    })
    fx.relay.enqueueBootstrap({
      device: PHONE_DEVICE_ID,
      events: [wire({ content: 'boot: baseline\n', cursor: 5 })],
      cursor: 6,
    })

    const started = await fx.engine.start()

    expect(started).toBe(true)
    expect(fx.relay.registerCalls).toHaveLength(1)
    expect(fx.relay.registerCalls[0]).toMatchObject({ deviceId: PHONE_DEVICE_ID, tier: 'phone' })
    expect(fx.store.loadConfigContent(ASSET_ID)).toBe('boot: baseline\n')
    expect(fx.store.loadCursor()).toBe(6)
    expect(fx.engine.status().bootstrapDone).toBe(true)
  })

  it('start：未配对 → 不起轮', async () => {
    const fx = makeEngine()
    expect(await fx.engine.start()).toBe(false)
    expect(fx.scheduleCalls).toEqual([])
  })

  it('pull：增量应用 + 水位推进到本页最大 cursor；空页不回退', async () => {
    const fx = makeEngine()
    stagedEnroll(fx.relay)
    fx.relay.enqueueBootstrap({ device: PHONE_DEVICE_ID, events: [], cursor: 2 })
    await fx.engine.joinWithAuth('jwt-admin')
    fx.relay.enqueuePull({
      events: [wire({ cursor: 3, content: 'a\n' }), wire({ cursor: 7, content: 'b\n' })],
      count: 2,
    })
    await fx.engine.syncNow()
    expect(fx.store.loadCursor()).toBe(7)
    fx.relay.enqueuePull({ events: [], count: 0 })
    await fx.engine.syncNow()
    expect(fx.store.loadCursor()).toBe(7)
    expect(fx.relay.pullCalls).toEqual([2, 7])
  })

  it('pull 失败：游标不动、心跳仍照发（错误隔离）', async () => {
    const fx = makeEngine()
    stagedEnroll(fx.relay)
    fx.relay.enqueueBootstrap({ device: PHONE_DEVICE_ID, events: [], cursor: 2 })
    await fx.engine.joinWithAuth('jwt-admin')
    fx.relay.failPull(new RelayError(0, 'relay 不可达'))
    await fx.engine.syncNow()
    expect(fx.store.loadCursor()).toBe(2)
    expect(fx.engine.status().lastError).toContain('relay 不可达')
    expect(fx.relay.heartbeatCalls).toHaveLength(1)
    expect(fx.store.loadLastHeartbeat()).not.toBeNull()
  })
})

describe('心跳上报（30s 周期）', () => {
  it('每拍心跳一次：设备/档位如实上报，heartbeat_at 落痕迹', async () => {
    const fx = makeEngine()
    stagedEnroll(fx.relay)
    fx.relay.enqueueBootstrap({ device: PHONE_DEVICE_ID, events: [], cursor: 1 })
    await fx.engine.joinWithAuth('jwt-admin')
    await fx.engine.syncNow()
    expect(fx.relay.heartbeatCalls).toEqual([
      { deviceId: PHONE_DEVICE_ID, tier: 'phone' },
    ])
    expect(fx.store.loadLastHeartbeat()).toBe('2026-10-17T00:00:30.000Z')
  })
})

describe('未配对防护', () => {
  it('未配对 syncNow / ingest 拒绝', async () => {
    const fx = makeEngine()
    await expect(fx.engine.syncNow()).rejects.toThrow('尚未配对')
    expect(await fx.engine.ingestWire(wire())).toBe('invalid')
  })
})
