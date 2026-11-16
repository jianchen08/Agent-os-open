/** @feature FP-0.2.多端同步 T3 前置·只读快照 UI | @ci: frontend-test */
/**
 * BackendConnectionPage（后端连接页）— 本机卡身份与探活 + 设备卡与地盘三态：
 * ①可达地盘（正常呈现）/ ②离线快照（显著标注「离线快照·截至 {时刻}·不可干活」
 * + 写硬边界置灰禁用）/ ③不可达未同步（诚实空态「该地盘不可达且无快照」）。
 * 文案口径断言：地盘/空间语言，无「星型/链式/兜底/节点/连接管理」运维词汇。
 * apiClient（HTTP）与 fetch（探活）按外部依赖 mock，query 走真实 TanStack Query。
 */
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SERVER_BASE_URL_KEY } from '@/services/shellApiBase'
import { renderWithProviders } from '@/test/renderWithProviders'
import { BackendConnectionPage } from '../BackendConnectionPage'
import type * as ApiConstants from '@/constants/api'
import type * as connectionProbeModule from '@/services/connectionProbe'

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
}))
vi.mock('@/services/api/client', () => ({ default: apiMock }))

const usePluginsQueryMock = vi.hoisted(() => vi.fn())
vi.mock('@/hooks/queries/usePluginsQuery', () => ({
  usePluginsQuery: (...args: unknown[]) => usePluginsQueryMock(...args),
}))

const fetchMock = vi.hoisted(() => vi.fn())
vi.stubGlobal('fetch', fetchMock)

const reloadMock = vi.hoisted(() => vi.fn())
Object.defineProperty(window, 'location', {
  value: { ...window.location, reload: reloadMock },
  writable: true,
})

const backendModeMock = vi.hoisted(() => ({ applyShellBackendMode: vi.fn() }))
// 连接探活桩：仅设备卡连接路径需要「探活必通」；置 null 时走真实现
// （本机卡探活 UI 测试依赖真实 probeConnection，不做全量覆盖）
const probeStub = vi.hoisted(() => ({ fn: null as null | (() => Promise<unknown>) }))
// 借权面 mock：设备卡连接走 borrowAndConnect（真实实现打本机内核，测试不打网络）
const borrowMock = vi.hoisted(() => ({ borrowAndConnect: vi.fn() }))
vi.mock('@/services/deviceBorrow', () => ({
  borrowAndConnect: (...args: unknown[]) => borrowMock.borrowAndConnect(...args),
  borrowSession: vi.fn(),
  mintLocalKernelToken: vi.fn(),
}))
vi.mock('@/services/connectionProbe', async (importOriginal) => {
  const actual = await importOriginal<typeof connectionProbeModule>()
  return {
    ...actual,
    probeConnection: vi.fn((...args: Parameters<typeof actual.probeConnection>) =>
      probeStub.fn ? probeStub.fn(...args) : actual.probeConnection(...args),
    ),
  }
})
vi.mock('@/services/shellBackendMode', () => ({
  applyShellBackendMode: (...args: unknown[]) =>
    backendModeMock.applyShellBackendMode(...args),
}))

/** 中枢卡「使用中」判定与 API_BASE_URL 字面比较，测试内钉住基址。 */
vi.mock('@/constants/api', async (importOriginal) => {
  const actual = await importOriginal<ApiConstants>()
  return { ...actual, API_BASE_URL: 'http://hub.example:9300' }
})

interface MirrorRecordLike {
  asset_key: string
  table: string
  pk: string
  row: Record<string, unknown>
  owner_device: string
  deleted: boolean
  updated_at: string
  view: { group: string; seq: number } | null
  hlc: { physical_ms: number; logical: number; device_id: string }
}

function mirrorSession(
  threadId: string,
  owner: string,
  hlcMs: number,
  deleted = false,
): MirrorRecordLike {
  return {
    asset_key: threadId,
    table: 'sessions',
    pk: threadId,
    row: { thread_id: threadId, origin: 'domain_event' },
    owner_device: owner,
    deleted,
    updated_at: '',
    view: null,
    hlc: { physical_ms: hlcMs, logical: 0, device_id: owner },
  }
}

function mirrorMessage(
  group: string,
  seq: number,
  role: string,
  content: string,
  owner: string,
  hlcMs: number,
): MirrorRecordLike {
  return {
    asset_key: `${group}/${role}/${seq}/${content}`,
    table: 'messages',
    pk: `${group}/${role}/${seq}`,
    row: { role, content },
    owner_device: owner,
    deleted: false,
    updated_at: '',
    view: { group, seq },
    hlc: { physical_ms: hlcMs, logical: 0, device_id: owner },
  }
}

type GetOpts = {
  devices?: unknown
  agentStatus?: unknown
  mirrorSessions?: unknown
  mirrorMessages?: unknown
}

function resolveOrReject(value: unknown): Promise<{ data: unknown }> {
  return value instanceof Error ? Promise.reject(value) : Promise.resolve({ data: value })
}

/** 挂载期 GET 按 URL + table 参数分发（镜像端点单端点按 table 过滤）。 */
function mockGets(opts: GetOpts) {
  apiMock.get.mockImplementation((url: string, config?: { params?: Record<string, string> }) => {
    if (url.endsWith('/ext/sync_agent/relay/devices')) return resolveOrReject(opts.devices)
    if (url.endsWith('/ext/sync_agent/status')) return resolveOrReject(opts.agentStatus)
    if (url.endsWith('/ext/sync_agent/mirror/records')) {
      const table = config?.params?.table
      if (table === 'sessions') return resolveOrReject(opts.mirrorSessions ?? { records: [] })
      if (table === 'messages') return resolveOrReject(opts.mirrorMessages ?? { records: [] })
      return Promise.reject(new Error(`unexpected mirror table ${String(table)}`))
    }
    return Promise.reject(new Error(`unexpected GET ${url}`))
  })
}

const PAIRED_PHONE = { loaded: true, paired: true, device_id: 'phone-b', tier: 'phone' }

/** 拓扑无类型学禁词（R14 裁定⑦ + R10 拍板⑥：用户唯一概念=地盘）。 */
const FORBIDDEN_WORDS = ['星型', '链式', '兜底', '节点', '连接管理']

function expectNoOpsVocabulary() {
  const text = document.body.textContent ?? ''
  for (const word of FORBIDDEN_WORDS) {
    expect(text, `页面文案不应出现运维词汇「${word}」`).not.toContain(word)
  }
}

afterEach(() => {
  vi.useRealTimers()
})

beforeEach(() => {
    probeStub.fn = null
  apiMock.get.mockReset()
  apiMock.post.mockReset()
  apiMock.post.mockResolvedValue({ data: { results: [] } })
  fetchMock.mockReset()
  reloadMock.mockReset()
  backendModeMock.applyShellBackendMode.mockReset()
  usePluginsQueryMock.mockReturnValue({ data: { plugins: [] } })
  localStorage.removeItem(SERVER_BASE_URL_KEY)
})

describe('BackendConnectionPage — 本机卡（身份 + 探活）', () => {
  it('已配对：本机身份带 device_id 与档位；未配对呈现引导', async () => {
    mockGets({ agentStatus: PAIRED_PHONE, devices: { devices: [] } })
    renderWithProviders(<BackendConnectionPage embedded />)
    // 本机卡在统一列表内（注册表驱动），随查询解析渲染
    const identity = await screen.findByTestId('device-self-identity')
    expect(identity).toHaveTextContent('phone-b')
    expect(identity).toHaveTextContent('phone')
  })

  it('本机同步状态不可用：诚实呈现，不冒充已配对', async () => {
    mockGets({ agentStatus: new Error('agent down'), devices: { devices: [] } })
    renderWithProviders(<BackendConnectionPage embedded />)
    expect(await screen.findByTestId('device-self-identity')).toHaveTextContent('不可用')
  })

  it('探活成功：在线（含耗时与内核版本）；探活目标为当前生效地址', async () => {
    mockGets({ agentStatus: PAIRED_PHONE, devices: { devices: [] } })
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      json: () => Promise.resolve({ status: 'ok', version: '0.2.10' }),
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('connection-test-button'))
    const result = await screen.findByTestId('connection-probe-result')
    await waitFor(() => {
      expect(result).toHaveTextContent('在线')
      expect(result).toHaveTextContent('内核 0.2.10')
    })
    expect(fetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/health'),
      expect.anything(),
    )
  })

  it('探活打当前生效地址；失败如实呈现离线', async () => {
    mockGets({ agentStatus: PAIRED_PHONE, devices: { devices: [] } })
    fetchMock.mockResolvedValue({
      status: 200,
      ok: true,
      json: () => Promise.resolve({ status: 'ok', version: 'v' }),
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('connection-test-button'))
    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith('http://hub.example:9300/health', expect.anything())
    })
    fetchMock.mockRejectedValue(new TypeError('network down'))
    fireEvent.click(await screen.findByTestId('connection-test-button'))
    await waitFor(() => {
      expect(screen.getByTestId('connection-probe-result')).toHaveTextContent('离线')
    })
  })
})

describe('BackendConnectionPage — 设备卡与可达性（拓扑无类型学）', () => {
  it('无地址卡走心跳语义：在线（心跳）/离线（心跳超窗）；本机不出现在设备卡列表', async () => {
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices: {
        devices: [
          { device_id: 'desktop-a', tier: 'desktop', app_version: '1.1.0', online: true },
          { device_id: 'laptop-c', tier: 'desktop', app_version: '1.1.0', online: false },
        ],
      },
    })
    const { container } = renderWithProviders(<BackendConnectionPage embedded />)
    const onlineCard = await screen.findByTestId('device-card-desktop-a')
    expect(within(onlineCard).getByTestId('device-card-reach-desktop-a')).toHaveTextContent(
      '在线（心跳）',
    )
    const offlineCard = await screen.findByTestId('device-card-laptop-c')
    expect(within(offlineCard).getByTestId('device-card-reach-laptop-c')).toHaveTextContent(
      '离线（心跳超窗）',
    )
    // 本端行渲染本机卡（注册表驱动统一列表；本机卡 testid 唯一）
    expect(screen.getByTestId('device-self-card')).toBeInTheDocument()
    expect(screen.queryByTestId('device-card-phone-b')).toBeNull()
    expect(screen.getByTestId('device-self-note')).toHaveTextContent('会话列表')
    expectNoOpsVocabulary()
    expect(container).toBeTruthy()
  })

  it('可达性语义分层（ADR 2026-10-21）：中枢卡拉取即可达；有地址卡实测探活；探活只打非中枢 id', async () => {
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices: {
        devices: [
          {
            device_id: 'cloud-c',
            tier: 'cloud',
            app_version: '1.0.0',
            online: false,
            is_hub: true,
          },
          {
            device_id: 'desktop-a',
            tier: 'desktop',
            app_version: '1.0.0',
            online: false,
            address: 'http://10.0.0.8:9100',
          },
        ],
      },
    })
    apiMock.post.mockResolvedValue({
      data: { results: [{ device_id: 'desktop-a', address: 'http://10.0.0.8:9100', ok: true, status: 200, latency_ms: 42 }] },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    const hubCard = await screen.findByTestId('device-card-cloud-c')
    // 中枢卡：心跳过期仍显示可达（拉取即证据，ADR 2026-10-21）
    expect(within(hubCard).getByTestId('device-card-reach-cloud-c')).toHaveTextContent('可达')
    await waitFor(() =>
      expect(screen.getByTestId('device-card-reach-desktop-a')).toHaveTextContent('可达 · 42ms'),
    )
    expect(apiMock.post).toHaveBeenCalledTimes(1)
    const [url, body] = apiMock.post.mock.calls[0]
    expect(String(url)).toMatch(/\/ext\/sync_agent\/probe$/)
    // 探活入参只含非中枢有地址设备
    expect(body).toEqual({ device_ids: ['desktop-a'] })
  })

  it('有地址卡实测不可达：显示「不可达（实测）」而非心跳话术', async () => {
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices: {
        devices: [
          {
            device_id: 'desktop-a',
            tier: 'desktop',
            app_version: '1.0.0',
            online: true,
            address: 'http://10.0.0.8:9100',
          },
        ],
      },
    })
    apiMock.post.mockResolvedValue({
      data: {
        results: [
          { device_id: 'desktop-a', address: 'http://10.0.0.8:9100', ok: false, status: 0, latency_ms: 3000, error: 'ConnectError' },
        ],
      },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    await waitFor(() =>
      expect(screen.getByTestId('device-card-reach-desktop-a')).toHaveTextContent('不可达（实测'),
    )
    const reach = screen.getByTestId('device-card-reach-desktop-a')
    // 实测不可达不走心跳话术
    expect(reach.textContent).not.toContain('心跳')
  })

  it('注册表不可达：诚实降级文案「暂不可见」', async () => {
    mockGets({ agentStatus: PAIRED_PHONE, devices: new Error('relay down') })
    renderWithProviders(<BackendConnectionPage embedded />)
    expect(await screen.findByTestId('territory-list-unavailable')).toHaveTextContent('暂不可见')
  })

  it('注册表为空：「尚无其他设备」入网引导', async () => {
    mockGets({ agentStatus: PAIRED_PHONE, devices: { devices: [] } })
    renderWithProviders(<BackendConnectionPage embedded />)
    expect(await screen.findByTestId('territory-list-empty')).toHaveTextContent('尚无其他设备')
  })

  it('本机同步状态不可用：注册表他端卡照常呈现', async () => {
    mockGets({
      agentStatus: new Error('agent down'),
      devices: {
        devices: [{ device_id: 'desktop-a', tier: 'desktop', app_version: '1.1.0', online: true }],
      },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    const card = await screen.findByTestId('device-card-desktop-a')
    expect(within(card).getByTestId('device-card-reach-desktop-a')).toHaveTextContent('在线（心跳）')
  })
})

describe('BackendConnectionPage — 卡内看地盘三态', () => {
  /** 本机=phone-b（不出现在卡列表）；远端在线=desktop-a；远端离线=laptop-c */
  const devices = {
    devices: [
      { device_id: 'desktop-a', tier: 'desktop', app_version: '1.1.0', online: true },
      { device_id: 'phone-b', tier: 'phone', app_version: '1.1.0', online: true },
      { device_id: 'laptop-c', tier: 'desktop', app_version: '1.1.0', online: false },
    ],
  }

  it('①可达地盘：展开在线设备显示其同步会话，无快照标注', async () => {
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices,
      mirrorSessions: {
        records: [mirrorSession('thread-1', 'desktop-a', 1000), mirrorSession('thread-2', 'desktop-a', 2000)],
      },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('device-card-toggle-desktop-a'))
    expect(await screen.findByTestId('territory-session-row-thread-1')).toBeInTheDocument()
    expect(screen.getByTestId('territory-session-row-thread-2')).toBeInTheDocument()
    expect(screen.queryByTestId('snapshot-banner')).toBeNull()
    expectNoOpsVocabulary()
  })

  it('②离线快照：离线设备有镜像会话 → 「离线快照」「不可干活」「截至」显著标注', async () => {
    const hlcMs = Date.UTC(2026, 9, 17, 6, 30, 0)
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices,
      mirrorSessions: { records: [mirrorSession('thread-9', 'laptop-c', hlcMs)] },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('device-card-toggle-laptop-c'))
    const banner = await screen.findByTestId('snapshot-banner')
    expect(banner).toHaveTextContent('离线快照')
    expect(banner).toHaveTextContent('不可干活')
    expect(banner).toHaveTextContent('截至')
    expect(screen.getByTestId('territory-session-row-thread-9')).toBeInTheDocument()
  })

  it('③不可达未同步：离线设备无镜像 → 诚实空态「该地盘不可达且无快照」', async () => {
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices,
      mirrorSessions: { records: [] },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('device-card-toggle-laptop-c'))
    expect(await screen.findByTestId('territory-empty')).toHaveTextContent('该地盘不可达且无快照')
  })

  it('在线空地盘与③区分：「暂无同步会话」，不出现快照话术', async () => {
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices,
      mirrorSessions: { records: [] },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('device-card-toggle-desktop-a'))
    const empty = await screen.findByTestId('territory-empty')
    expect(empty).toHaveTextContent('暂无同步会话')
    expect(empty.textContent).not.toContain('不可达')
  })

  it('墓碑会话不呈现（镜像 deleted 行过滤）', async () => {
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices,
      mirrorSessions: {
        records: [mirrorSession('thread-live', 'desktop-a', 1000), mirrorSession('thread-dead', 'desktop-a', 2000, true)],
      },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('device-card-toggle-desktop-a'))
    expect(await screen.findByTestId('territory-session-row-thread-live')).toBeInTheDocument()
    expect(screen.queryByTestId('territory-session-row-thread-dead')).toBeNull()
  })

  it('镜像面不可用：诚实呈现「快照数据暂不可见」，不冒充空态', async () => {
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices,
      mirrorSessions: new Error('mirror down'),
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('device-card-toggle-desktop-a'))
    expect(await screen.findByTestId('territory-snapshot-error')).toHaveTextContent('快照数据暂不可见')
  })

  it('归属过滤：只呈现该设备自己的镜像会话', async () => {
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices,
      mirrorSessions: {
        records: [mirrorSession('thread-a-owned', 'desktop-a', 1000), mirrorSession('thread-other', 'laptop-c', 1000)],
      },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('device-card-toggle-desktop-a'))
    expect(await screen.findByTestId('territory-session-row-thread-a-owned')).toBeInTheDocument()
    expect(screen.queryByTestId('territory-session-row-thread-other')).toBeNull()
  })
})

describe('BackendConnectionPage — 会话详情与写硬边界（断链=写硬边界）', () => {
  const devices = {
    devices: [
      { device_id: 'desktop-a', tier: 'desktop', app_version: '1.1.0', online: true },
      { device_id: 'phone-b', tier: 'phone', app_version: '1.1.0', online: true },
      { device_id: 'laptop-c', tier: 'desktop', app_version: '1.1.0', online: false },
    ],
  }

  it('②快照详情：横幅+消息置灰（data-dimmed）+ 发送输入与按钮 disabled', async () => {
    const hlcMs = Date.UTC(2026, 9, 17, 6, 30, 0)
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices,
      mirrorSessions: { records: [mirrorSession('thread-9', 'laptop-c', hlcMs)] },
      mirrorMessages: {
        records: [mirrorMessage('thread-9', 1, 'user', '你好', 'laptop-c', hlcMs)],
      },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('device-card-toggle-laptop-c'))
    fireEvent.click(await screen.findByTestId('territory-session-row-thread-9'))
    const banner = await screen.findByTestId('snapshot-banner')
    expect(banner).toHaveTextContent('不可干活')
    const messages = await screen.findByTestId('territory-messages')
    expect(messages).toHaveAttribute('data-dimmed', 'true')
    expect(screen.getByTestId('snapshot-input')).toBeDisabled()
    expect(screen.getByTestId('snapshot-send')).toBeDisabled()
  })

  it('①可达详情：消息按 view.seq 升序渲染（乱序喂入断顺序），无写边界面', async () => {
    const hlcMs = Date.UTC(2026, 9, 17, 6, 30, 0)
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices,
      mirrorSessions: { records: [mirrorSession('thread-1', 'desktop-a', hlcMs)] },
      mirrorMessages: {
        records: [
          mirrorMessage('thread-1', 2, 'assistant', '第二条', 'desktop-a', hlcMs),
          mirrorMessage('thread-1', 1, 'user', '第一条', 'desktop-a', hlcMs),
        ],
      },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('device-card-toggle-desktop-a'))
    fireEvent.click(await screen.findByTestId('territory-session-row-thread-1'))
    const messages = await screen.findByTestId('territory-messages')
    expect(within(messages).getByText('第一条')).toBeInTheDocument()
    expect(within(messages).getByText('第二条')).toBeInTheDocument()
    expect(messages.textContent.indexOf('第一条')).toBeLessThan(messages.textContent.indexOf('第二条'))
    expect(messages).toHaveAttribute('data-dimmed', 'false')
    expect(screen.queryByTestId('snapshot-banner')).toBeNull()
    expect(screen.queryByTestId('snapshot-send')).toBeNull()
  })

  it('详情只呈现该会话（view.group=会话 id）的消息，他组不串', async () => {
    const hlcMs = Date.UTC(2026, 9, 17, 6, 30, 0)
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices,
      mirrorSessions: { records: [mirrorSession('thread-1', 'desktop-a', hlcMs)] },
      mirrorMessages: {
        records: [
          mirrorMessage('thread-1', 1, 'user', '本组消息', 'desktop-a', hlcMs),
          mirrorMessage('thread-other', 1, 'user', '别组消息', 'desktop-a', hlcMs),
        ],
      },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('device-card-toggle-desktop-a'))
    fireEvent.click(await screen.findByTestId('territory-session-row-thread-1'))
    const messages = await screen.findByTestId('territory-messages')
    expect(within(messages).getByText('本组消息')).toBeInTheDocument()
    expect(within(messages).queryByText('别组消息')).toBeNull()
  })

  it('无同步消息的会话详情：「该会话暂无同步消息」', async () => {
    const hlcMs = Date.UTC(2026, 9, 17, 6, 30, 0)
    mockGets({
      agentStatus: PAIRED_PHONE,
      devices,
      mirrorSessions: { records: [mirrorSession('thread-1', 'desktop-a', hlcMs)] },
      mirrorMessages: { records: [] },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    fireEvent.click(await screen.findByTestId('device-card-toggle-desktop-a'))
    fireEvent.click(await screen.findByTestId('territory-session-row-thread-1'))
    expect(await screen.findByTestId('territory-messages-empty')).toHaveTextContent('该会话暂无同步消息')
  })
})

describe('BackendConnectionPage — 配对中枢卡（一机一卡，无特殊卡型）', () => {
  it('所连内核 relay 源站为回环（loopback 自配对）→ 中枢卡无连接按钮（禁死按钮）', async () => {
    mockGets(
      {
        agentStatus: {
          loaded: true,
          paired: true,
          device_id: 'phone-b',
          tier: 'phone',
          relay_url: 'http://127.0.0.1:9100/ext/sync_relay',
        },
        devices: {
          devices: [
            { device_id: 'vm-hub', tier: 'desktop', app_version: '1.1.0', online: true, is_hub: true },
          ],
        },
      },
    )
    renderWithProviders(<BackendConnectionPage embedded />)
    await screen.findByTestId('device-card-vm-hub')
    expect(screen.queryByTestId('device-card-remote')).toBeNull()
    expect(screen.queryByTestId('device-card-connect-vm-hub')).toBeNull()
  })

  it('已配对未连接：中枢卡带免登连接按钮，点击走 borrowAndConnect（is_hub=true）', async () => {
    mockGets(
      {
        agentStatus: {
          loaded: true,
          paired: true,
          device_id: 'phone-b',
          tier: 'phone',
          relay_url: 'http://other.example:9300/ext/sync_relay',
        },
        devices: {
          devices: [
            { device_id: 'vm-hub', tier: 'desktop', app_version: '1.1.0', online: true, is_hub: true },
          ],
        },
      },
    )
    renderWithProviders(<BackendConnectionPage embedded />)
    const button = await screen.findByTestId('device-card-connect-vm-hub')
    vi.useFakeTimers()
    borrowMock.borrowAndConnect.mockResolvedValue({ ok: true })
    await act(async () => {
      fireEvent.click(button)
    })
    expect(borrowMock.borrowAndConnect).toHaveBeenCalledWith('http://other.example:9300', true)
    vi.useRealTimers()
  })

  it('当前生效即中枢源站：中枢卡置顶（列表第一张）高亮带「已连接」', async () => {
    localStorage.setItem(SERVER_BASE_URL_KEY, 'http://hub.example:9300')
    mockGets(
      {
        agentStatus: {
          loaded: true,
          paired: true,
          device_id: 'phone-b',
          tier: 'phone',
          relay_url: 'http://hub.example:9300/ext/sync_relay',
        },
        devices: {
          devices: [
            { device_id: 'vm-hub', tier: 'desktop', app_version: '1.1.0', online: true, is_hub: true },
            { device_id: 'laptop-c', tier: 'desktop', app_version: '1.1.0', online: false },
          ],
        },
      },
    )
    const { container } = renderWithProviders(<BackendConnectionPage embedded />)
    const badge = await screen.findByTestId('device-card-connected-vm-hub')
    expect(badge).toHaveTextContent('已连接')
    const firstCard = container.querySelector('[data-testid="territory-list"] > div')
    expect(firstCard?.getAttribute('data-testid')).toBe('device-card-vm-hub')
  })

  it('连接本机时：本机卡置顶高亮带「已连接」', async () => {
    mockGets(
      {
        agentStatus: {
          loaded: true,
          paired: true,
          device_id: 'phone-b',
          tier: 'phone',
          relay_url: 'http://other.example:9300/ext/sync_relay',
        },
        devices: { devices: [] },
      },
    )
    const { container } = renderWithProviders(<BackendConnectionPage embedded />)
    expect(await screen.findByTestId('self-connected-badge')).toHaveTextContent('已连接')
    const firstCard = container.querySelector('[data-testid="territory-list"] > div')
    expect(firstCard?.getAttribute('data-testid')).toBe('device-self-card')
  })
})

describe('BackendConnectionPage — 注册表设备卡连接与储存（自报面上卡）', () => {
  const devicesWithAddress = {
    devices: [
      {
        device_id: 'desktop-a',
        tier: 'desktop',
        app_version: '1.1.0',
        online: true,
        address: 'http://192.168.1.10:9100',
        storage: { free_bytes: 12_000_000_000, total_bytes: 64_000_000_000 },
      },
      { device_id: 'laptop-c', tier: 'desktop', app_version: '1.1.0', online: false },
    ],
  }

  it('带地址设备卡呈现「连接此内核」，点击写基址+上报 remote+刷新', async () => {
    mockGets({ agentStatus: PAIRED_PHONE, devices: devicesWithAddress })
    renderWithProviders(<BackendConnectionPage embedded />)
    const button = await screen.findByTestId('device-card-connect-desktop-a')
    vi.useFakeTimers()
    probeStub.fn = async () => ({ ok: true, status: 200, kernelVersion: '0.2.2', latencyMs: 5 })
    borrowMock.borrowAndConnect.mockResolvedValue({ ok: true })
    await act(async () => {
      fireEvent.click(button)
    })
    expect(borrowMock.borrowAndConnect).toHaveBeenCalledWith(
      'http://192.168.1.10:9100',
      undefined,
    )
  })

  it('中枢卡注册表无地址（loopback 自配对已清）→ 回落配对中枢源站给连接按钮', async () => {
    mockGets({
      agentStatus: {
        ...PAIRED_PHONE,
        relay_url: 'http://162.14.69.153/ext/sync_relay',
      },
      devices: {
        devices: [
          {
            device_id: 'vm-hub',
            tier: 'desktop',
            app_version: '1.1.0',
            online: true,
            is_hub: true,
          },
        ],
      },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    const button = await screen.findByTestId('device-card-connect-vm-hub')
    vi.useFakeTimers()
    borrowMock.borrowAndConnect.mockResolvedValue({ ok: true })
    await act(async () => {
      fireEvent.click(button)
    })
    expect(borrowMock.borrowAndConnect).toHaveBeenCalledWith('http://162.14.69.153', true)
    vi.useRealTimers()
  })

  it('无地址设备卡无连接入口（不编数据）；带地址卡显示储存行', async () => {
    mockGets({ agentStatus: PAIRED_PHONE, devices: devicesWithAddress })
    renderWithProviders(<BackendConnectionPage embedded />)
    await screen.findByTestId('device-card-desktop-a')
    expect(screen.queryByTestId('device-card-connect-laptop-c')).toBeNull()
    const card = screen.getByTestId('device-card-desktop-a')
    expect(within(card).getByText(/剩余 11\.2G \/ 59\.6G/)).toBeInTheDocument()
  })

  it('本机卡呈现自报储存行', async () => {
    mockGets({
      agentStatus: {
        ...PAIRED_PHONE,
        storage: { free_bytes: 5_000_000_000, total_bytes: 32_000_000_000 },
      },
      devices: { devices: [] },
    })
    renderWithProviders(<BackendConnectionPage embedded />)
    await waitFor(() => {
      expect(screen.getByTestId('device-self-card')).toHaveTextContent('剩余 4.7G / 29.8G')
    })
  })
})
