// @feature: FP-0.2.四 前端Schema(插件宿主 widget) | @ci: frontend-test
/**
 * PluginHostsWidget 测试（监控页「插件」tab 进程观测视图）
 *
 * 断行为：三态（加载/空/错误+重试）、进程卡片渲染（标题按插件名派生：
 * 独占=插件名/共享=N 个插件共享进程/无成员=进程标识）、概要行、成员插件
 * 状态词（运行中/启动中蓝/等待重新加载黄/已停止红/快照缺失不上词，迁移自
 * 原「插件运行」表状态列）、上次崩溃 join 列（有值显示/0 与缺失不显示/运行
 * 端点失败降级提示不阻塞主视图）、卡片级红色警示三分支（进程未响应已停止/
 * 调用长时间未返回疑似卡住/启动迟迟未完成）、「未装载」区渲染、底部折叠区
 * （已停止卡片与未装载默认收起不占主视野，展开可见，计数报头，全空不渲染）、
 * 汇总条全口径内存分段（插件段求和/内核段 memstats/应用壳段 Electron IPC，
 * 全应用合计仅三段全有值时给出，缺段显 — 不虚构）。
 * mock 仅用于外部 API 请求层（services/api/pluginHosts、
 * services/api/systemMetrics）与 window.electronAPI；时钟用
 * toFake:['Date'] 钉死「现在」，定时器保持真实（react-query/RTL 正常工作）。
 */

import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { PluginHostsWidget } from '../PluginHostsWidget'
import { renderWithProviders } from '@/test/renderWithProviders'
import type { PluginHost, PluginRuntimeRow } from '@/types/pluginHosts'
import type { PluginHostsSnapshot } from '@/services/api/pluginHosts'

const mockGetPluginHosts = vi.fn<() => Promise<PluginHostsSnapshot>>()
const mockGetPluginRuntimeRows = vi.fn<() => Promise<PluginRuntimeRow[]>>()
const mockGetKernelMemStats = vi.fn<() => Promise<{ process_rss_bytes: number | null }>>()
const mockGetAppMetrics = vi.fn<() => Promise<{ processCount: number; totalWorkingSetKb: number }>>()

vi.mock('@/services/api/pluginHosts', () => ({
  getPluginHosts: () => mockGetPluginHosts(),
  getPluginRuntimeRows: () => mockGetPluginRuntimeRows(),
}))

vi.mock('@/services/api/systemMetrics', () => ({
  getKernelMemStats: () => mockGetKernelMemStats(),
}))

/** 内核段默认值：104857600 字节 = 100 MB 精确（换算断言用） */
const KERNEL_RSS_BYTES = 104857600
/** 应用壳段默认值：716800 KB = 700 MB 精确（换算断言用） */
const SHELL_WORKING_SET_KB = 716800

/** 挂/摘 window.electronAPI（Web 形态缺失路径用例摘除） */
function setElectronApi(api: unknown) {
  ;(window as unknown as { electronAPI?: unknown }).electronAPI = api
}

/** 钉死「现在」：2026-09-27T12:00:00 本地时间 */
const NOW = new Date('2026-09-27T12:00:00')
/** NOW 的 unix 秒 */
const NOW_SECS = NOW.getTime() / 1000

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] })
  vi.setSystemTime(NOW)
  mockGetPluginHosts.mockReset()
  mockGetPluginRuntimeRows.mockReset()
  mockGetKernelMemStats.mockReset()
  mockGetAppMetrics.mockReset()
  // 运行态 join 源默认空清单（无崩溃记录）；关注 join 的用例单独覆写
  mockGetPluginRuntimeRows.mockResolvedValue([])
  // 全口径内存两段默认有值（内核 100 MB / 应用壳 700 MB）；缺段用例单独覆写
  mockGetKernelMemStats.mockResolvedValue({ process_rss_bytes: KERNEL_RSS_BYTES })
  mockGetAppMetrics.mockResolvedValue({ processCount: 5, totalWorkingSetKb: SHELL_WORKING_SET_KB })
  setElectronApi({ appMetrics: { get: () => mockGetAppMetrics() } })
})

afterEach(() => {
  vi.useRealTimers()
  setElectronApi(undefined)
})

/** 健康共享进程：普通成员 + 等待重新加载成员 + 启动中成员；调用新鲜（1 分钟前） */
function healthyGroupHost(): PluginHost {
  return {
    host_key: 'group:light:1',
    kind: 'group',
    pid: 1234,
    alive: true,
    rss_mb: 56.2,
    uptime_secs: 3600,
    spawned_members: ['bash_tool', 'search', 'metrics'],
    members: [
      { plugin_id: 'bash_tool', pending_rejoin: false },
      { plugin_id: 'search', pending_rejoin: true },
      { plugin_id: 'metrics', pending_rejoin: false },
    ],
    in_flight: 1,
    member_in_flight: { bash_tool: 1 },
    last_call_at: NOW_SECS - 60,
    starting: false,
    starting_members: ['metrics'],
  }
}

/** 异常独占进程：进程死亡 + 缺 pid/rss/uptime（契约允许 null） */
function deadSoloHost(): PluginHost {
  return {
    host_key: 'solo:metrics_admin',
    kind: 'solo',
    pid: null,
    alive: false,
    rss_mb: null,
    uptime_secs: null,
    spawned_members: [],
    members: [{ plugin_id: 'metrics_admin', pending_rejoin: true }],
    in_flight: 0,
    member_in_flight: {},
    last_call_at: null,
    starting: false,
    starting_members: [],
  }
}

/** 展开底部「已停止/未装载」折叠区（非运行面默认收起；先等数据渲染出折叠头再点击） */
async function expandStoppedPending() {
  fireEvent.click(await screen.findByTestId('stopped-pending-toggle'))
}

describe('PluginHostsWidget — 三态', () => {
  it('首拉未返回 → 加载骨架屏', () => {
    mockGetPluginHosts.mockReturnValue(new Promise(() => {}))
    renderWithProviders(<PluginHostsWidget />)
    expect(document.querySelector('.animate-pulse')).not.toBeNull()
  })

  it('全空（无进程无未装载）→ 空态文案，不渲染卡片与未装载区', async () => {
    mockGetPluginHosts.mockResolvedValue({ hosts: [], pending_spawn: [] })
    renderWithProviders(<PluginHostsWidget />)
    expect(await screen.findByText('暂无插件运行数据')).toBeInTheDocument()
    expect(screen.queryByTestId('host-card')).not.toBeInTheDocument()
    expect(screen.queryByTestId('pending-spawn')).not.toBeInTheDocument()
  })

  it('请求失败 → 可读错误 + 重试成功后恢复渲染', async () => {
    mockGetPluginHosts.mockRejectedValueOnce(new Error('内核端点 503'))
    renderWithProviders(<PluginHostsWidget />)
    expect(await screen.findByText('内核端点 503')).toBeInTheDocument()

    mockGetPluginHosts.mockResolvedValue({ hosts: [healthyGroupHost()], pending_spawn: [] })
    fireEvent.click(screen.getByRole('button', { name: '重试' }))
    expect(await screen.findByTestId('host-card')).toBeInTheDocument()
    await waitFor(() => expect(mockGetPluginHosts).toHaveBeenCalledTimes(2))
  })

  it('非 Error 抛出物 → 兜底文案', async () => {
    mockGetPluginHosts.mockRejectedValueOnce('boom')
    renderWithProviders(<PluginHostsWidget />)
    expect(await screen.findByText('获取插件运行状态失败')).toBeInTheDocument()
  })
})

describe('PluginHostsWidget — 卡片渲染（健康共享进程 + 异常独占进程组合）', () => {
  beforeEach(() => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [healthyGroupHost(), deadSoloHost()],
      pending_spawn: [],
    })
  })

  it('卡片标题：共享进程报「N 个插件共享进程」，独占进程直接用插件名', async () => {
    renderWithProviders(<PluginHostsWidget />)
    // 主视野只有运行中卡片；已停止进程收进底部折叠区（默认收起）
    const cards = await screen.findAllByTestId('host-card')
    expect(cards).toHaveLength(1)
    expect(within(cards[0]!).getByTestId('host-card-title')).toHaveTextContent('3 个插件共享进程')

    await expandStoppedPending()
    const stoppedCard = screen.getAllByTestId('host-card')[1]!
    // 独占进程标题 = 成员插件名，内核形态标识（solo:*）不出现在界面
    expect(within(stoppedCard).getByTestId('host-card-title')).toHaveTextContent('metrics_admin')
    expect(within(stoppedCard).queryByText(/solo:/)).not.toBeInTheDocument()
  })

  it('概要行：PID/内存/运行时长/进行中的调用/最后调用逐项渲染（进程信息为次要）', async () => {
    renderWithProviders(<PluginHostsWidget />)
    await expandStoppedPending()
    const cards = await screen.findAllByTestId('host-card')

    const healthy = within(cards[0]!)
    // 「运行中」两处：卡头进程徽标（alive=true）+ bash_tool 成员状态词
    expect(healthy.getAllByText('运行中')).toHaveLength(2)
    expect(healthy.getByText('PID 1234')).toBeInTheDocument()
    expect(healthy.getByText('内存 56.2 MB')).toBeInTheDocument()
    expect(healthy.getByText('运行 1小时0分')).toBeInTheDocument()
    expect(healthy.getByText('进行中的调用 1')).toBeInTheDocument()
    expect(healthy.getByText('最后调用 1分钟前')).toBeInTheDocument()

    const dead = within(cards[1]!)
    // 契约允许 null：缺值显示 —，不猜测
    expect(dead.getByText('PID —')).toBeInTheDocument()
    expect(dead.getByText('内存 —')).toBeInTheDocument()
    expect(dead.getByText('运行 —')).toBeInTheDocument()
    expect(dead.getByText('最后调用 —')).toBeInTheDocument()
  })

  it('成员状态词：运行中/等待重新加载黄标/启动中蓝标/已停止红标', async () => {
    renderWithProviders(<PluginHostsWidget />)
    await expandStoppedPending()
    await screen.findAllByTestId('host-card')
    const members = screen.getAllByTestId('host-members')[0]!
    const running = within(members).getByText('bash_tool').closest('span')
    expect(running).toHaveTextContent('运行中')
    expect(running).toHaveClass('bg-accent/30')
    const rejoin = within(members).getByText('search').closest('span')
    expect(rejoin).toHaveTextContent('等待重新加载')
    expect(rejoin).toHaveClass('bg-status-warning/10')
    const starting = within(members).getByText('metrics').closest('span')
    expect(starting).toHaveTextContent('启动中')
    expect(starting).toHaveClass('bg-status-info/10')
    // 死进程的独占成员：软卸载标记（pending_rejoin）优先于进程死亡 → 等待重新加载
    const deadMembers = screen.getAllByTestId('host-members')[1]!
    const deadRejoin = within(deadMembers).getByText('metrics_admin').closest('span')
    expect(deadRejoin).toHaveTextContent('等待重新加载')
    expect(deadRejoin).toHaveClass('bg-status-warning/10')
  })

  it('成员已分配但进程实际成员不含（非重加入标记）→ 已停止红标', async () => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [
        {
          ...healthyGroupHost(),
          members: [{ plugin_id: 'orphan', pending_rejoin: false }],
          spawned_members: ['bash_tool'],
        },
      ],
      pending_spawn: [],
    })
    renderWithProviders(<PluginHostsWidget />)
    const card = (await screen.findAllByTestId('host-card'))[0]!
    const chip = within(within(card).getByTestId('host-members')).getByText('orphan').closest('span')
    expect(chip).toHaveTextContent('已停止')
    expect(chip).toHaveClass('bg-status-error/10')
  })

  it('调用新鲜（1 分钟前）不触发卡住警示；死亡进程（展开折叠区后）打红警示', async () => {
    renderWithProviders(<PluginHostsWidget />)
    const cards = await screen.findAllByTestId('host-card')
    expect(within(cards[0]!).queryByTestId('host-alerts')).not.toBeInTheDocument()
    await expandStoppedPending()
    const stoppedCard = screen.getAllByTestId('host-card')[1]!
    expect(within(stoppedCard).getByTestId('host-alerts')).toHaveTextContent('进程未响应，已停止')
  })
})

describe('PluginHostsWidget — 上次崩溃 join 列（运行态端点）', () => {
  async function renderWithRuntime(rows: PluginRuntimeRow[], runtimeError = false) {
    mockGetPluginHosts.mockResolvedValue({ hosts: [healthyGroupHost()], pending_spawn: [] })
    if (runtimeError) {
      mockGetPluginRuntimeRows.mockRejectedValue(new Error('运行态端点 503'))
    } else {
      mockGetPluginRuntimeRows.mockResolvedValue(rows)
    }
    renderWithProviders(<PluginHostsWidget />)
    return (await screen.findAllByTestId('host-card'))[0]!
  }

  it('崩溃记录非零 → 成员行显示「上次崩溃」相对时间', async () => {
    const card = await renderWithRuntime([
      { plugin_id: 'bash_tool', alive: 1, last_crash_ts: NOW_SECS - 300 },
    ])
    const members = within(card).getByTestId('host-members')
    expect(within(members).getByText('bash_tool').closest('span')).toHaveTextContent(
      '上次崩溃 5分钟前',
    )
    // 无崩溃记录的成员不显示该列
    expect(within(members).getByText('search').closest('span')).not.toHaveTextContent('上次崩溃')
  })

  it('崩溃时间戳 0（未崩过/留存窗外）与非数值 → 不显示崩溃列', async () => {
    const card = await renderWithRuntime([
      { plugin_id: 'bash_tool', last_crash_ts: 0 },
      { plugin_id: 'search', last_crash_ts: null },
    ])
    const members = within(card).getByTestId('host-members')
    expect(within(members).queryByText(/上次崩溃/)).not.toBeInTheDocument()
  })

  it('运行态端点失败 → 卡片照常渲染 + 底部降级提示（不阻塞主视图）', async () => {
    const card = await renderWithRuntime([], true)
    expect(within(card).getByTestId('host-card-title')).toHaveTextContent('3 个插件共享进程')
    expect(await screen.findByTestId('runtime-degraded')).toHaveTextContent('崩溃记录暂不可用')
  })

  it('运行态端点正常 → 无降级提示', async () => {
    await renderWithRuntime([])
    expect(screen.queryByTestId('runtime-degraded')).not.toBeInTheDocument()
  })
})

describe('PluginHostsWidget — 卡片级红色警示分支', () => {
  function hostWith(overrides: Partial<PluginHost>): PluginHost {
    return { ...healthyGroupHost(), ...overrides }
  }

  async function renderSingle(host: PluginHost) {
    mockGetPluginHosts.mockResolvedValue({ hosts: [host], pending_spawn: [] })
    renderWithProviders(<PluginHostsWidget />)
    return (await screen.findAllByTestId('host-card'))[0]!
  }

  it('调用卡住：进行中的调用>0 且最后调用距今 >10 分钟 → 疑似卡住警示', async () => {
    const card = await renderSingle(hostWith({ last_call_at: NOW_SECS - 11 * 60 }))
    const alerts = within(card).getByTestId('host-alerts')
    expect(alerts).toHaveTextContent('调用长时间未返回，疑似卡住')
    expect(alerts).toHaveTextContent('11分钟前')
  })

  it('调用未卡住：进行中的调用>0 但最后调用距今 5 分钟 → 无警示（10 分钟判定窗边界内）', async () => {
    const card = await renderSingle(hostWith({ last_call_at: NOW_SECS - 5 * 60 }))
    expect(within(card).queryByTestId('host-alerts')).not.toBeInTheDocument()
  })

  it('进行中的调用>0 但从未调用（last_call_at=null）→ 无法判定卡住，不误报', async () => {
    const card = await renderSingle(hostWith({ last_call_at: null }))
    expect(within(card).queryByTestId('host-alerts')).not.toBeInTheDocument()
  })

  it('启动迟迟未完成 → 红警示', async () => {
    const card = await renderSingle(hostWith({ starting: true }))
    expect(within(card).getByTestId('host-alerts')).toHaveTextContent('进程启动中迟迟未完成')
  })

  it('多警示并存 → 逐条列出（已停止进程在底部折叠区，展开可见）', async () => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [hostWith({ alive: false, starting: true })],
      pending_spawn: [],
    })
    renderWithProviders(<PluginHostsWidget />)
    await expandStoppedPending()
    const card = screen.getAllByTestId('host-card')[0]!
    const alerts = within(card).getByTestId('host-alerts')
    expect(alerts).toHaveTextContent('进程未响应，已停止')
    expect(alerts).toHaveTextContent('进程启动中迟迟未完成')
  })
})

describe('PluginHostsWidget — 概要格式化分支', () => {
  it('运行时长 天/分/秒三段人读时长（同契约三组有区分度输入）', async () => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [
        { ...healthyGroupHost(), host_key: 'h:day', uptime_secs: 90061 },
        { ...healthyGroupHost(), host_key: 'h:min', uptime_secs: 300 },
        { ...healthyGroupHost(), host_key: 'h:sec', uptime_secs: 45 },
      ],
      pending_spawn: [],
    })
    renderWithProviders(<PluginHostsWidget />)
    const cards = await screen.findAllByTestId('host-card')
    expect(within(cards[0]!).getByText('运行 1天1小时')).toBeInTheDocument()
    expect(within(cards[1]!).getByText('运行 5分')).toBeInTheDocument()
    expect(within(cards[2]!).getByText('运行 45秒')).toBeInTheDocument()
  })

  it('卡片标题按成员数派生：单成员=插件名 / 多成员=共享 / 无成员=进程标识；内核形态值不漏出', async () => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [
        { ...healthyGroupHost(), host_key: 'solo:alpha', kind: 'weird', members: [{ plugin_id: 'alpha' }] },
        { ...healthyGroupHost(), host_key: 'group:pair', members: [{ plugin_id: 'p1' }, { plugin_id: 'p2' }] },
        { ...healthyGroupHost(), host_key: 'solo:orphan', members: [] },
      ],
      pending_spawn: [],
    })
    renderWithProviders(<PluginHostsWidget />)
    const cards = await screen.findAllByTestId('host-card')
    expect(within(cards[0]!).getByTestId('host-card-title')).toHaveTextContent('alpha')
    expect(within(cards[0]!).queryByText('weird')).not.toBeInTheDocument()
    expect(within(cards[1]!).getByTestId('host-card-title')).toHaveTextContent('2 个插件共享进程')
    expect(within(cards[2]!).getByTestId('host-card-title')).toHaveTextContent('solo:orphan')
  })

  it('进行中的调用/members 为 null → 调用按 0 显示、无成员徽标、不误报卡住', async () => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [{ ...healthyGroupHost(), host_key: 'n:null', in_flight: null, members: null }],
      pending_spawn: [],
    })
    renderWithProviders(<PluginHostsWidget />)
    const card = (await screen.findAllByTestId('host-card'))[0]!
    expect(within(card).getByText('进行中的调用 0')).toBeInTheDocument()
    expect(within(card).getByTestId('host-members')).toBeEmptyDOMElement()
    expect(within(card).queryByTestId('host-alerts')).not.toBeInTheDocument()
  })

  it('成员实际快照缺失（spawned_members=null）→ 状态词不上（不猜测），普通样式', async () => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [{ ...healthyGroupHost(), spawned_members: null }],
      pending_spawn: [],
    })
    renderWithProviders(<PluginHostsWidget />)
    const card = (await screen.findAllByTestId('host-card'))[0]!
    const members = within(card).getByTestId('host-members')
    const running = within(members).getByText('bash_tool').closest('span')
    expect(running).toHaveTextContent('bash_tool')
    expect(running).not.toHaveTextContent('运行中')
    expect(running).not.toHaveTextContent('已停止')
    expect(running).toHaveClass('bg-accent/30')
  })
})

describe('PluginHostsWidget — 未装载区（底部折叠区内，展开可见）', () => {
  it('未装载区红显（区名 + 说明 + 插件 id + 最后调用）', async () => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [],
      pending_spawn: [
        { plugin_id: 'metrics_admin', enabled: true, last_call_at: null },
        { plugin_id: 'ghost_plugin', enabled: true, last_call_at: NOW_SECS - 120 },
      ],
    })
    renderWithProviders(<PluginHostsWidget />)
    // 默认收起：折叠头只报计数，未装载区不在 DOM
    expect(await screen.findByTestId('stopped-pending-toggle')).toHaveTextContent('未装载 2')
    expect(screen.queryByTestId('pending-spawn')).not.toBeInTheDocument()

    await expandStoppedPending()
    const section = screen.getByTestId('pending-spawn')
    expect(within(section).getByText('未装载')).toBeInTheDocument()
    expect(within(section).getByText('已启用但当前没有运行的进程')).toBeInTheDocument()
    expect(within(section).getByText('metrics_admin')).toBeInTheDocument()
    expect(within(section).getByText('ghost_plugin')).toBeInTheDocument()
    expect(within(section).getByText('最后调用 —')).toBeInTheDocument()
    expect(within(section).getByText('最后调用 2分钟前')).toBeInTheDocument()
    // 无进程但有未装载 → 不算全空
    expect(screen.queryByText('暂无插件运行数据')).not.toBeInTheDocument()
  })

  it('无未装载插件 → 不渲染未装载区', async () => {
    mockGetPluginHosts.mockResolvedValue({ hosts: [healthyGroupHost()], pending_spawn: [] })
    renderWithProviders(<PluginHostsWidget />)
    await screen.findByTestId('host-card')
    expect(screen.queryByTestId('pending-spawn')).not.toBeInTheDocument()
  })
})

describe('PluginHostsWidget — 底部折叠区（非运行面收进最下）', () => {
  it('默认收起：主视野只有运行中卡片，折叠头报「已停止/未装载」计数', async () => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [healthyGroupHost(), deadSoloHost()],
      pending_spawn: [{ plugin_id: 'ghost_plugin', enabled: true, last_call_at: null }],
    })
    renderWithProviders(<PluginHostsWidget />)
    await screen.findByTestId('host-card')
    const toggle = screen.getByTestId('stopped-pending-toggle')
    expect(toggle).toHaveTextContent('已停止 1 · 未装载 1')
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getAllByTestId('host-card')).toHaveLength(1)
    expect(screen.queryByTestId('pending-spawn')).not.toBeInTheDocument()
  })

  it('点击展开：已停止卡片与未装载区出现（aria-expanded 翻转）；再点收起', async () => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [healthyGroupHost(), deadSoloHost()],
      pending_spawn: [{ plugin_id: 'ghost_plugin', enabled: true, last_call_at: null }],
    })
    renderWithProviders(<PluginHostsWidget />)
    await screen.findByTestId('host-card')

    await expandStoppedPending()
    const toggle = screen.getByTestId('stopped-pending-toggle')
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getAllByTestId('host-card')).toHaveLength(2)
    expect(screen.getByTestId('pending-spawn')).toBeInTheDocument()

    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getAllByTestId('host-card')).toHaveLength(1)
    expect(screen.queryByTestId('pending-spawn')).not.toBeInTheDocument()
  })

  it('全部运行且无未装载 → 折叠区整体不渲染', async () => {
    mockGetPluginHosts.mockResolvedValue({ hosts: [healthyGroupHost()], pending_spawn: [] })
    renderWithProviders(<PluginHostsWidget />)
    await screen.findByTestId('host-card')
    expect(screen.queryByTestId('stopped-pending')).not.toBeInTheDocument()
  })

  it('仅已停止无未装载 → 折叠头只报已停止计数', async () => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [healthyGroupHost(), deadSoloHost()],
      pending_spawn: [],
    })
    renderWithProviders(<PluginHostsWidget />)
    const toggle = await screen.findByTestId('stopped-pending-toggle')
    expect(toggle).toHaveTextContent('已停止 1')
    expect(toggle).not.toHaveTextContent('未装载')
  })
})

describe('PluginHostsWidget — 汇总条（进程计数 + 全口径内存分段）', () => {
  /** 健康独占进程：rss 100.4 + 单成员（与 healthy/dead 组成三态输入） */
  function runningSoloHost(): PluginHost {
    return {
      host_key: 'solo:llm_service',
      kind: 'solo',
      pid: 5678,
      alive: true,
      rss_mb: 100.4,
      uptime_secs: 60,
      spawned_members: ['llm_service'],
      members: [{ plugin_id: 'llm_service', pending_rejoin: false }],
      in_flight: 0,
      member_in_flight: {},
      last_call_at: NOW_SECS - 30,
      starting: false,
      starting_members: [],
    }
  }

  it('汇总条：进程/状态计数 + 全口径分段（插件求和/内核/应用壳/全应用合计，null 不进求和）', async () => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [healthyGroupHost(), runningSoloHost(), deadSoloHost()],
      pending_spawn: [
        { plugin_id: 'ghost_plugin', enabled: true, last_call_at: null },
        { plugin_id: 'never_called', enabled: true, last_call_at: null },
      ],
    })
    renderWithProviders(<PluginHostsWidget />)
    const summary = await screen.findByTestId('hosts-summary')
    // 进程总数含已停止；alive 三态计数（true=运行中 / false=已停止 / null 不计词）
    expect(within(summary).getByText('进程 3')).toBeInTheDocument()
    expect(within(summary).getByText('运行中 2')).toBeInTheDocument()
    expect(within(summary).getByText('已停止 1')).toBeInTheDocument()
    // 插件段 = 56.2 + 100.4（deadSoloHost 的 null 不进求和）
    expect(within(summary).getByText('插件内存 156.6 MB')).toBeInTheDocument()
    // 内核段 104857600B→100.0 MB / 应用壳段 716800KB→700.0 MB
    expect(within(summary).getByText('内核 100.0 MB')).toBeInTheDocument()
    expect(within(summary).getByText('应用壳 700.0 MB')).toBeInTheDocument()
    // 全应用 = 156.6 + 100 + 700（三段全有值才合计）
    expect(within(summary).getByText('全应用 956.6 MB')).toBeInTheDocument()
    // 承载插件 = 3 + 1 + 1
    expect(within(summary).getByText('承载插件 5')).toBeInTheDocument()
    expect(within(summary).getByText('未装载 2')).toBeInTheDocument()
  })

  it('全部宿主 rss 缺失 → 插件内存/全应用显 —（内核壳有值也不虚构合计）', async () => {
    mockGetPluginHosts.mockResolvedValue({
      hosts: [deadSoloHost()],
      pending_spawn: [],
    })
    renderWithProviders(<PluginHostsWidget />)
    const summary = await screen.findByTestId('hosts-summary')
    expect(within(summary).getByText('插件内存 —')).toBeInTheDocument()
    expect(within(summary).getByText('内核 100.0 MB')).toBeInTheDocument()
    expect(within(summary).getByText('应用壳 700.0 MB')).toBeInTheDocument()
    expect(within(summary).getByText('全应用 —')).toBeInTheDocument()
    // 无未装载 → 汇总条不出未装载词
    expect(within(summary).queryByText(/未装载/)).not.toBeInTheDocument()
  })

  it('Web 形态（无 electronAPI）→ 应用壳/全应用显 —，插件与内核段照常', async () => {
    setElectronApi(undefined)
    mockGetPluginHosts.mockResolvedValue({
      hosts: [healthyGroupHost()],
      pending_spawn: [],
    })
    renderWithProviders(<PluginHostsWidget />)
    const summary = await screen.findByTestId('hosts-summary')
    expect(within(summary).getByText('插件内存 56.2 MB')).toBeInTheDocument()
    expect(within(summary).getByText('内核 100.0 MB')).toBeInTheDocument()
    expect(within(summary).getByText('应用壳 —')).toBeInTheDocument()
    expect(within(summary).getByText('全应用 —')).toBeInTheDocument()
  })

  it('内核段缺失（process_rss_bytes=null）→ 内核/全应用显 —，不虚构合计', async () => {
    mockGetKernelMemStats.mockResolvedValue({ process_rss_bytes: null })
    mockGetPluginHosts.mockResolvedValue({
      hosts: [healthyGroupHost()],
      pending_spawn: [],
    })
    renderWithProviders(<PluginHostsWidget />)
    const summary = await screen.findByTestId('hosts-summary')
    expect(within(summary).getByText('插件内存 56.2 MB')).toBeInTheDocument()
    expect(within(summary).getByText('内核 —')).toBeInTheDocument()
    expect(within(summary).getByText('应用壳 700.0 MB')).toBeInTheDocument()
    expect(within(summary).getByText('全应用 —')).toBeInTheDocument()
  })
})
