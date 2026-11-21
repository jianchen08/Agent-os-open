/** @feature FP-0.2.多端同步 设备卡连接面补测 | @ci: frontend-test */
/**
 * 设备卡连接面补测（SelfDeviceCard / PeerDeviceCards）：
 * - 本机卡「切换」入口：自定义档使用中 → 切回本机内核的成功/失败提示
 * - 对端卡可达性回落：探活报「地址不可用」→ 如实回落心跳语义（不冒充实测）
 * - 配对中枢地址非法（解析失败）→ 不产出直连入口（安全回退 null）
 * - 卡上直连失败 → 错误就地呈现；「仍要切换」兜底 → 直连登录页并清错
 *
 * mock 边界仅外部依赖：同步/镜像查询（HTTP 数据源）、连接切换单点（写存储 +
 * 原生栈动作 + 整页刷新）、借权连接（打对端网络）。领域纯函数（快照时刻/储存
 * 文案/回环判定）全走真实实现。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SERVER_BASE_URL_KEY } from '@/services/shellApiBase'
import { PeerDeviceCards } from '../PeerDeviceCards'
import { SelfDeviceCard } from '../SelfDeviceCard'
import type { ConnectKernelResult } from '../connectKernel'
import type { DeviceProbeResult, RelayDevice, RelayDevicesResponse, SyncAgentStatus } from '@/services/api/syncMirror'

// 同步面数据源（HTTP）：按用例给数据，避免真实网络与查询缓存时序
const mirror = vi.hoisted(() => ({
  status: undefined as SyncAgentStatus | undefined,
  devices: undefined as RelayDevicesResponse | undefined,
  probes: undefined as DeviceProbeResult[] | undefined,
}))

vi.mock('@/hooks/queries/useSyncMirrorQuery', () => ({
  useSyncAgentStatusQuery: () => ({ data: mirror.status }),
  useRelayDevicesQuery: () => ({ data: mirror.devices }),
  useDeviceProbesQuery: () => ({ data: mirror.probes }),
  useMirrorSessionsQuery: () => ({ data: undefined, isError: false }),
  useMirrorMessagesQuery: () => ({ data: undefined, isError: false }),
}))

// 连接切换单点（写存储 + 原生兜底 + 整页刷新）：动作结果由用例给定
const connect = vi.hoisted(() => ({ fn: vi.fn<(address: string) => Promise<ConnectKernelResult>>() }))
vi.mock('@/pages/settings/devices/connectKernel', () => ({
  connectKernel: (address: string) => connect.fn(address),
}))

// 借权连接（打对端网络）：结果由用例给定
const borrow = vi.hoisted(() => ({ fn: vi.fn() }))
vi.mock('@/services/deviceBorrow', async (importOriginal) => ({
  ...(await importOriginal<object>()),
  borrowAndConnect: (...args: unknown[]) => borrow.fn(...args),
}))

function makeDevice(overrides: Partial<RelayDevice> = {}): RelayDevice {
  return {
    device_id: 'peer-a',
    tier: 'standard',
    app_version: '0.2.2',
    online: true,
    last_heartbeat_at: '2026-10-28T10:00:00Z',
    ...overrides,
  }
}

function makeStatus(overrides: Partial<SyncAgentStatus> = {}): SyncAgentStatus {
  return { loaded: true, device_id: 'self-1', tier: 'standard', ...overrides }
}

function makeDevices(devices: RelayDevice[]): RelayDevicesResponse {
  return { devices, source: 'cloud', degraded: false }
}

beforeEach(() => {
  localStorage.clear()
  mirror.status = makeStatus()
  mirror.devices = makeDevices([])
  mirror.probes = []
  connect.fn.mockReset()
  borrow.fn.mockReset()
})

afterEach(() => {
  localStorage.clear()
  vi.restoreAllMocks()
})

// ────────────────────────── SelfDeviceCard：切回本机内核 ──────────────────────────

describe('SelfDeviceCard — 切回本机内核', () => {
  it('自定义档使用中：点「切换」→ 连接本机档成功，提示本机栈启动中', async () => {
    localStorage.setItem(SERVER_BASE_URL_KEY, 'http://192.168.1.20:9300')
    connect.fn.mockResolvedValue({ ok: true })

    render(<SelfDeviceCard connected={false} />)

    fireEvent.click(screen.getByTestId('backend-use-local-button'))

    await waitFor(() =>
      expect(screen.getByTestId('connection-saved-hint')).toHaveTextContent('已切回本机内核'),
    )
    expect(connect.fn).toHaveBeenCalledWith('')
  })

  it('自定义档使用中：连接本机档失败 → 就地呈现失败原因（不假成功）', async () => {
    localStorage.setItem(SERVER_BASE_URL_KEY, 'http://192.168.1.21:9300')
    connect.fn.mockResolvedValue({ ok: false, error: '本机栈启动失败' })

    render(<SelfDeviceCard connected={false} />)

    fireEvent.click(screen.getByTestId('backend-use-local-button'))

    await waitFor(() =>
      expect(screen.getByTestId('connection-saved-hint')).toHaveTextContent('本机栈启动失败'),
    )
  })

  it('本机档使用中：呈现「使用中」而非切换入口（本地模式零切换动作）', () => {
    render(<SelfDeviceCard connected />)

    expect(screen.getByTestId('backend-mode-local')).toHaveTextContent('使用中')
    expect(screen.queryByTestId('backend-use-local-button')).not.toBeInTheDocument()
    expect(screen.getByTestId('self-connected-badge')).toBeInTheDocument()
  })
})

// ────────────────────────── PeerDeviceCards：可达性三态与直连 ──────────────────────────

describe('PeerDeviceCards — 可达性回落与卡上直连', () => {
  it('探活报地址不可用 → 回落心跳语义；中枢地址非法 → 不产出直连入口', () => {
    mirror.status = makeStatus({ paired: true, relay_url: '::not-a-url::' })
    mirror.devices = makeDevices([makeDevice({ device_id: 'peer-nat', address: 'http://10.0.0.9:9300' })])
    mirror.probes = [
      { device_id: 'peer-nat', address: null, ok: false, status: 0, latency_ms: 0, error: 'no_address' },
    ]

    render(<PeerDeviceCards />)

    expect(screen.getByTestId('device-card-reach-peer-nat')).toHaveTextContent('在线（心跳）')
    // 卡片存在且有地址 → 仍给「连接此内核」；但中枢侧不合成直连（地址非法）
    expect(screen.getByTestId('device-card-connect-peer-nat')).toBeInTheDocument()
  })

  it('卡上直连失败 → 错误就地呈现；「仍要切换」→ 直连登录页并清错', async () => {
    mirror.devices = makeDevices([makeDevice({ device_id: 'peer-b', address: 'http://10.0.0.10:9300' })])
    mirror.probes = [
      { device_id: 'peer-b', address: 'http://10.0.0.10:9300', ok: true, status: 200, latency_ms: 12 },
    ]
    borrow.fn.mockResolvedValue({ ok: false, error: '借权被拒绝', canRetryManual: true })
    connect.fn.mockResolvedValue({ ok: true })

    render(<PeerDeviceCards />)

    fireEvent.click(screen.getByTestId('device-card-connect-peer-b'))

    await waitFor(() =>
      expect(screen.getByTestId('device-card-connect-error-peer-b')).toHaveTextContent('借权被拒绝'),
    )

    fireEvent.click(screen.getByTestId('device-card-connect-fallback-peer-b'))

    await waitFor(() =>
      expect(screen.queryByTestId('device-card-connect-error-peer-b')).not.toBeInTheDocument(),
    )
    expect(connect.fn).toHaveBeenCalledWith('http://10.0.0.10:9300')
  })

  it('注册表暂无其他设备 → 显式空态（不伪装可达）', () => {
    mirror.devices = makeDevices([])

    render(<PeerDeviceCards />)

    expect(screen.getByTestId('territory-list-empty')).toBeInTheDocument()
  })
})
