import { ConfigStore } from '../configStore'
import { CredentialStore } from '../credential'
import { LocalStorageKv } from '../storage'
import type { SyncEngineDeps, RelayTransport } from '../syncEngine'
import { SyncEngine } from '../syncEngine'
import type {
  BootstrapPage,
  DeviceRegistration,
  EnrollOutcome,
  EnrollRequest,
  PullPage,
} from '../relayClient'
import { RelayError } from '../relayClient'

/** 最小 Storage 形状（LocalStorageKv 仅消费 getItem/setItem/removeItem/key/length）。 */
export function memoryStorage(): Storage {
  const map = new Map<string, string>()
  const impl = {
    getItem: (key: string) => (map.has(key) ? map.get(key)! : null),
    setItem: (key: string, value: string) => void map.set(key, String(value)),
    removeItem: (key: string) => void map.delete(key),
    key: (index: number) => [...map.keys()][index] ?? null,
    get length() {
      return map.size
    },
    clear: () => void map.clear(),
  }
  return impl as unknown as Storage
}

export function memoryKv(): { kv: LocalStorageKv; storage: Storage } {
  const storage = memoryStorage()
  return { kv: new LocalStorageKv(storage), storage }
}

interface EnrollCall {
  relayUrl: string
  jwt: string
  deviceId: string
  tier: string
}

/** relay 传输替身：记录调用、按队列回放响应/错误（外部依赖桩）。 */
export class RecordingRelay implements RelayTransport {
  enrollCalls: EnrollCall[] = []
  registerCalls: DeviceRegistration[] = []
  heartbeatCalls: Array<{ deviceId: string; tier: string }> = []
  pullCalls: number[] = []
  bootstrapCalls = 0

  private enrollOutcome: EnrollOutcome | null = null
  private enrollError: RelayError | null = null
  private bootstrapQueue: BootstrapPage[] = []
  private pullQueue: PullPage[] = []
  private pullError: RelayError | null = null
  private heartbeatAt = '2026-10-17T00:00:30.000Z'

  resolveEnroll(outcome: EnrollOutcome): void {
    this.enrollOutcome = outcome
    this.enrollError = null
  }

  rejectEnroll(error: RelayError): void {
    this.enrollError = error
    this.enrollOutcome = null
  }

  enqueueBootstrap(page: BootstrapPage): void {
    this.bootstrapQueue.push(page)
  }

  enqueuePull(page: PullPage): void {
    this.pullQueue.push(page)
  }

  failPull(error: RelayError): void {
    this.pullError = error
  }

  async enrollWithJwt(
    relayUrl: string,
    jwt: string,
    request: EnrollRequest,
  ): Promise<EnrollOutcome> {
    this.enrollCalls.push({
      relayUrl,
      jwt,
      deviceId: request.deviceId,
      tier: request.tier,
    })
    if (this.enrollError) throw this.enrollError
    if (!this.enrollOutcome) throw new RelayError(500, 'enroll outcome not staged')
    return this.enrollOutcome
  }

  async register(_relayUrl: string, _token: string, registration: DeviceRegistration): Promise<void> {
    this.registerCalls.push(registration)
  }

  async heartbeat(
    _relayUrl: string,
    _token: string,
    registration: { deviceId: string; tier: string },
  ): Promise<string> {
    this.heartbeatCalls.push({ deviceId: registration.deviceId, tier: registration.tier })
    return this.heartbeatAt
  }

  async pullEvents(_relayUrl: string, _token: string, sinceCursor: number): Promise<PullPage> {
    this.pullCalls.push(sinceCursor)
    if (this.pullError) throw this.pullError
    return this.pullQueue.shift() ?? { events: [], count: 0 }
  }

  async bootstrap(_relayUrl: string, _token: string): Promise<BootstrapPage> {
    this.bootstrapCalls += 1
    return this.bootstrapQueue.shift() ?? { device: 'phone-1', events: [], cursor: 0 }
  }
}

export const PHONE_DEVICE_ID = '11111111-2222-4333-8444-555555555555'
export const RELAY_URL = 'http://relay.example/ext/sync_relay'
export const JOIN_RELAY_URL = 'http://shell.example/ext/sync_relay'

export interface EngineFixture {
  engine: SyncEngine
  store: ConfigStore
  credential: CredentialStore
  relay: RecordingRelay
  kv: LocalStorageKv
  scheduleCalls: Array<{ ms: number }>
  cancelled: boolean[]
}

export function makeEngine(overrides: Partial<SyncEngineDeps> = {}): EngineFixture {
  const { kv } = memoryKv()
  const store = new ConfigStore(kv)
  store.saveDeviceId(PHONE_DEVICE_ID) // 设备 UUID 跨用例固定（join 兑换/回显判定都以它为锚）
  const credential = new CredentialStore(kv, null)
  const relay = new RecordingRelay()
  const scheduleCalls: Array<{ ms: number }> = []
  const cancelled: boolean[] = []
  const schedule = (tick: () => void, ms: number) => {
    scheduleCalls.push({ ms })
    void tick
    return () => void cancelled.push(true)
  }
  const engine = new SyncEngine({
    credential,
    store,
    client: relay,
    joinRelayUrl: () => JOIN_RELAY_URL,
    schedule,
    clockMs: () => 1_760_000_000_000,
    nowIso: () => '2026-10-17T00:00:00.000Z',
    appVersion: '0.2.2-test',
    ...overrides,
  })
  return { engine, store, credential, relay, kv, scheduleCalls, cancelled }
}

export function stagedEnroll(relay: RecordingRelay, deviceId = PHONE_DEVICE_ID): void {
  relay.resolveEnroll({
    deviceId,
    deviceToken: 'token-phone-1',
    tenantId: 'tenant-1',
    relayUrl: RELAY_URL,
  })
}
