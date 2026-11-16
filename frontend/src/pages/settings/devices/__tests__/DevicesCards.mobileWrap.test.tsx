/** @feature FP-0.2.多端同步 设备卡移动端适配（X17-F1）| @ci: frontend-test */
/**
 * 设备卡移动端适配类断言（X17-F1：P-162/P-163/P-164，375 视口防御类）：
 * - P-162 对端卡头部：flex-wrap 折行 + device_id break-all（无空格长 token）+ meta min-w-0
 *   ——长 UUID（64 字符）不再把主操作钮推出视口
 * - P-163 镜像消息长 token（JSON/base64/URL）：whitespace-pre-wrap + break-words
 * - P-164 本机卡生效地址（长 URL）：min-w-0 + break-all
 *
 * 断言口径=类名存在性（换行行为归 CSS 引擎，jsdom 无法断言几何；静态锁防回归）。
 * mock 边界同 DevicesCards.residual：仅 HTTP 数据源与连接动作面，领域纯函数走真实实现。
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PeerDeviceCards } from '../PeerDeviceCards'
import { SelfDeviceCard } from '../SelfDeviceCard'
import type {
  DeviceProbeResult,
  MirrorRecord,
  RelayDevice,
  RelayDevicesResponse,
  SyncAgentStatus,
} from '@/services/api/syncMirror'

// 同步面数据源（HTTP）：按用例给数据
const mirror = vi.hoisted(() => ({
  status: undefined as SyncAgentStatus | undefined,
  devices: undefined as RelayDevicesResponse | undefined,
  probes: undefined as DeviceProbeResult[] | undefined,
  records: undefined as MirrorRecord[] | undefined,
}))

vi.mock('@/hooks/queries/useSyncMirrorQuery', () => ({
  useSyncAgentStatusQuery: () => ({ data: mirror.status }),
  useRelayDevicesQuery: () => ({ data: mirror.devices }),
  useDeviceProbesQuery: () => ({ data: mirror.probes }),
  useMirrorSessionsQuery: () => ({
    data: mirror.records === undefined ? undefined : { records: mirror.records },
    isError: false,
  }),
  useMirrorMessagesQuery: () => ({
    data: mirror.records === undefined ? undefined : { records: mirror.records },
    isError: false,
  }),
}))

// 连接切换单点与借权连接（网络面）：本文件只断言静态类，动作不发生
const connect = vi.hoisted(() => ({ fn: vi.fn() }))
vi.mock('@/pages/settings/devices/connectKernel', () => ({
  connectKernel: (address: string) => connect.fn(address),
}))
vi.mock('@/services/deviceBorrow', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  borrowAndConnect: vi.fn(),
}))

/** 64 字符 hex（UUID 等长的无空格 token：break-words 无断点，须 break-all） */
const LONG_ID = '0123456789abcdef'.repeat(4)
const LONG_TOKEN = 'https://example.invalid/very/long/path?token='.concat('a1b2c3d4e5'.repeat(8))

function makeDevice(overrides: Partial<RelayDevice> = {}): RelayDevice {
  return {
    device_id: LONG_ID,
    tier: 'standard',
    app_version: '0.2.2',
    online: true,
    last_heartbeat_at: '2026-11-04T10:00:00Z',
    ...overrides,
  }
}

function makeDevices(devices: RelayDevice[]): RelayDevicesResponse {
  return { devices, source: 'cloud', degraded: false }
}

function makeRecord(partial: Partial<MirrorRecord>): MirrorRecord {
  return {
    asset_key: 'k-1',
    table: 'messages',
    pk: 'pk-1',
    row: {},
    owner_device: LONG_ID,
    deleted: false,
    updated_at: '2026-11-04T00:00:00Z',
    view: null,
    hlc: { physical_ms: 0, logical: 0, device_id: LONG_ID },
    ...partial,
  }
}

beforeEach(() => {
  localStorage.clear()
  mirror.status = { loaded: true, device_id: 'self-1', tier: 'standard' }
  mirror.devices = makeDevices([])
  mirror.probes = []
  mirror.records = undefined
  connect.fn.mockReset()
})

afterEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
})

describe('PeerDeviceCards — 移动端换行防御类（P-162/P-163）', () => {
  it('长 UUID（64 字符）：头部 flex-wrap、device_id break-all、meta min-w-0', () => {
    mirror.devices = makeDevices([makeDevice()])

    render(<PeerDeviceCards />)

    const header = screen.getByTestId(`device-card-reach-${LONG_ID}`).parentElement as HTMLElement
    expect(header).toHaveClass('flex-wrap')
    const idSpan = screen.getByText(LONG_ID)
    expect(idSpan).toHaveClass('break-all')
    const metaSpan = idSpan.nextElementSibling as HTMLElement
    expect(metaSpan).toHaveClass('min-w-0')
  })

  it('镜像消息长 URL token：whitespace-pre-wrap + break-words 在位', () => {
    mirror.devices = makeDevices([makeDevice()])
    mirror.records = [
      makeRecord({ table: 'sessions', pk: 'sess-1', asset_key: 's-1' }),
      makeRecord({
        table: 'messages',
        pk: 'msg-1',
        asset_key: 'm-1',
        view: { group: 'sess-1', seq: 1 },
        row: { role: 'user', content: LONG_TOKEN },
      }),
    ]

    render(<PeerDeviceCards />)
    fireEvent.click(screen.getByTestId(`device-card-toggle-${LONG_ID}`))
    fireEvent.click(screen.getByTestId('territory-session-row-sess-1'))

    const tokenSpan = screen.getByText(LONG_TOKEN)
    expect(tokenSpan).toHaveClass('whitespace-pre-wrap', 'break-words')
  })
})

describe('SelfDeviceCard — 生效地址断行（P-164）', () => {
  it('生效地址 span：min-w-0 + break-all（长 URL 溢出防御）', () => {
    render(<SelfDeviceCard connected={false} />)

    expect(screen.getByTestId('connection-effective-base')).toHaveClass('min-w-0', 'break-all')
  })
})
