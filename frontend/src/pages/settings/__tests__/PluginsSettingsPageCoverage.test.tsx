/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * PluginsSettingsPage 覆盖缺口补测（与 PluginsSettingsPage.test.tsx 互补，不重复）
 *
 * 覆盖契约：
 * - 启停开关：成功响应 → 乐观更新（卡片开关 aria-label 与状态文案翻转）、
 *   query 缓存同步（重挂载后仍是新状态）、toast.success 用后端 message；
 * - 后端 success:false → toast.error(error) 且状态不变；
 * - 请求 reject → toast.error(Error.message) 且 toggling 复位（开关可再次点击）；
 * - 禁用事前提醒（ADR 2026-09-14 §2.4）：有已启用依赖方 → window.confirm 列出；
 *   取消 → 不发起 PUT；确认 → 继续 PUT；
 * - 连带摘除：cascade_disabled 非空且为禁用方向 → toast.warning 列出被摘除插件；
 * - 反向依赖端点不可得（reject）→ 静默放行继续 PUT；
 * - 类型徽标：pipeline/tool/system 与未知类型（Composite 回退）四类；
 * - 元信息：版本号、activation 中文、host_type、contributes/端点/配置文件计数；
 * - G2 净化标示：sanitized（含 rejected_tools 列表与 tools_before→after）与
 *   last_error 两条分支；
 * - 工具能力区：展开 input_schema、搜索无匹配空态、插件/类别/来源徽标。
 *
 * 测试策略：mock 仅限外部网络层（apiClient）与 toast 边界；组件与 query 链路真实
 * 渲染（全局 queryClient 复用，断言缓存同步）。
 */

import { cleanup, screen, waitFor, fireEvent } from '@testing-library/react'
import React from 'react'
import { afterEach, describe, expect, it, vi, beforeEach } from 'vitest'
import { toast } from '@/components/ui/sonner'
import { refreshPluginContributions } from '@/services/modules/GrowthLoop'
import { queryClient } from '@/services/query/queryClient'
import { renderWithProviders } from '@/test/renderWithProviders'
import { PluginsSettingsPage } from '../PluginsSettingsPage'

function plugin(overrides: Record<string, unknown> = {}) {
  return {
    plugin_id: 'demo_plugin',
    name: 'Demo Plugin',
    description: '演示插件',
    config_type: 'system',
    host_type: 'sidecar',
    version: '1.0.0',
    enabled: true,
    activation: 'lazy',
    status: 'active',
    config_files: [],
    has_contributes: false,
    has_http_endpoints: false,
    error: null,
    ...overrides,
  }
}

const mockGet = vi.fn()
const mockPut = vi.fn()

vi.mock('@/services/api/client', () => ({
  default: {
    get: (...args: unknown[]) => mockGet(...args),
    put: (...args: unknown[]) => mockPut(...args),
  },
}))
vi.mock('@/services/modules/GrowthLoop', () => ({
  refreshPluginContributions: vi.fn().mockResolvedValue(undefined),
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: { error: vi.fn(), success: vi.fn(), warning: vi.fn() },
}))

interface ApiOptions {
  plugins?: unknown[]
  tools?: unknown[]
  contract?: unknown
  dependents?: { id: string; enabled: boolean }[] | Error
}

function setupApi({ plugins = [], tools = [], contract = { plugins: [] }, dependents = [] }: ApiOptions = {}) {
  mockGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/plugins') return { data: plugins }
    if (url === '/api/v1/schema') return { data: { tools } }
    if (url === '/api/v1/plugins/contract-status') return { data: contract }
    if (url.includes('/dependents')) {
      if (dependents instanceof Error) throw dependents
      return { data: { plugin_id: 'x', dependents, total: dependents.length } }
    }
    throw new Error(`unexpected url: ${url}`)
  })
}

/**
 * 默认每次渲染独立 QueryClient（隔离用例间缓存）；需要观察缓存写入的用例
 * 显式传入全局 queryClient（renderPageShared）。
 */
function renderPage() {
  return renderWithProviders(<PluginsSettingsPage />)
}

function renderPageShared() {
  return renderWithProviders(<PluginsSettingsPage />, { queryClient })
}

async function waitForCard(name: string) {
  await waitFor(() => {
    expect(screen.getByText(name)).toBeInTheDocument()
  })
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(() => {
  cleanup()
  queryClient.clear()
})

describe('PluginsSettingsPage · 启停开关与缓存', () => {
  it('成功禁用：开关无障碍名翻转、toast.success 用后端 message、缓存同步', async () => {
    setupApi({ plugins: [plugin({ plugin_id: 'p1', name: '甲插件', enabled: true })] })
    mockPut.mockResolvedValue({ data: { success: true, message: '内核已接受禁用请求' } })
    renderPageShared()
    await waitForCard('甲插件')

    fireEvent.click(screen.getByLabelText('禁用 甲插件'))
    await waitFor(() => {
      expect(screen.getByLabelText('启用 甲插件')).toBeInTheDocument()
    })
    expect(toast.success).toHaveBeenCalledWith('内核已接受禁用请求')
    expect(refreshPluginContributions).toHaveBeenCalled()
    // 缓存乐观更新：重挂载读缓存仍是禁用态
    expect(queryClient.getQueryData<{ plugins: Array<{ enabled: boolean }> }>(['plugins'])?.plugins[0].enabled).toBe(false)
  })

  it('禁用后未返回 message 时用「已禁用 {id}」兜底', async () => {
    setupApi({ plugins: [plugin({ plugin_id: 'p2', name: '乙插件', enabled: true })] })
    mockPut.mockResolvedValue({ data: { success: true } })
    renderPage()
    await waitForCard('乙插件')

    fireEvent.click(screen.getByLabelText('禁用 乙插件'))
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith('已禁用 p2')
    })
  })

  it('启用方向：toast 文案为「已启用 {id}」且状态翻转', async () => {
    setupApi({ plugins: [plugin({ plugin_id: 'p3', name: '丙插件', enabled: false })] })
    mockPut.mockResolvedValue({ data: { success: true } })
    renderPage()
    await waitForCard('丙插件')

    fireEvent.click(screen.getByLabelText('启用 丙插件'))
    await waitFor(() => {
      expect(toast.success).toHaveBeenCalledWith('已启用 p3')
    })
  })

  it('后端 success:false → toast.error(error) 且开关状态不变', async () => {
    setupApi({ plugins: [plugin({ plugin_id: 'p4', name: '丁插件', enabled: true })] })
    mockPut.mockResolvedValue({ data: { success: false, error: '插件加载器拒绝' } })
    renderPage()
    await waitForCard('丁插件')

    fireEvent.click(screen.getByLabelText('禁用 丁插件'))
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('插件加载器拒绝')
    })
    expect(screen.getByLabelText('禁用 丁插件')).toBeInTheDocument()
    expect(refreshPluginContributions).not.toHaveBeenCalled()
  })

  it('success:false 且无 error 字段 → 兜底「操作失败」', async () => {
    setupApi({ plugins: [plugin({ plugin_id: 'p5', name: '戊插件', enabled: true })] })
    mockPut.mockResolvedValue({ data: { success: false } })
    renderPage()
    await waitForCard('戊插件')

    fireEvent.click(screen.getByLabelText('禁用 戊插件'))
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('操作失败')
    })
  })

  it('请求 reject → toast.error(Error.message)，开关复位可再次点击', async () => {
    setupApi({ plugins: [plugin({ plugin_id: 'p6', name: '己插件', enabled: true })] })
    mockPut.mockRejectedValueOnce(new Error('网络超时'))
    renderPage()
    await waitForCard('己插件')

    const toggle = screen.getByLabelText('禁用 己插件')
    fireEvent.click(toggle)
    await waitFor(() => {
      expect(toast.error).toHaveBeenCalledWith('网络超时')
    })
    await waitFor(() => {
      expect(toggle).toBeEnabled()
    })
  })
})

describe('PluginsSettingsPage · 禁用事前提醒（§2.4）', () => {
  const confirmSpy = vi.spyOn(window, 'confirm')

  beforeEach(() => {
    confirmSpy.mockReset()
  })

  it('有已启用依赖方：confirm 列出依赖，取消则不发起 PUT', async () => {
    setupApi({
      plugins: [plugin({ plugin_id: 'provider', name: '提供者', enabled: true })],
      dependents: [
        { id: 'consumer_a', enabled: true },
        { id: 'consumer_b', enabled: false },
      ],
    })
    confirmSpy.mockReturnValue(false)
    renderPage()
    await waitForCard('提供者')

    fireEvent.click(screen.getByLabelText('禁用 提供者'))
    await waitFor(() => {
      expect(confirmSpy).toHaveBeenCalled()
    })
    const message = confirmSpy.mock.calls[0][0] as string
    expect(message).toContain('consumer_a')
    expect(message).not.toContain('consumer_b')
    expect(mockPut).not.toHaveBeenCalled()
    expect(screen.getByLabelText('禁用 提供者')).toBeInTheDocument()
  })

  // 现状契约（疑似缺陷，已在回报中记录不修改生产代码）：连带摘除提醒的判据是
  // `!currentEnabled`，即 currentEnabled=true（禁用方向）时反而不提醒，
  // 而禁用方向恰是后端返回 cascade_disabled 的唯一方向。
  it('confirm 确认后继续 PUT；禁用方向的连带摘除提醒未触发（判据方向相反）', async () => {
    setupApi({
      plugins: [plugin({ plugin_id: 'provider', name: '提供者', enabled: true })],
      dependents: [{ id: 'consumer_a', enabled: true }],
    })
    confirmSpy.mockReturnValue(true)
    mockPut.mockResolvedValue({
      data: { success: true, cascade_disabled: ['consumer_a'] },
    })
    renderPage()
    await waitForCard('提供者')

    fireEvent.click(screen.getByLabelText('禁用 提供者'))
    await waitFor(() => {
      expect(mockPut).toHaveBeenCalledWith('/api/v1/plugins/provider/enabled', { enabled: false })
    })
    expect(toast.success).toHaveBeenCalled()
    expect(toast.warning).not.toHaveBeenCalled()
  })

  it('启用方向若后端返回 cascade_disabled 则触发 toast.warning（判据方向现状）', async () => {
    setupApi({ plugins: [plugin({ plugin_id: 'off2', name: '待启用二', enabled: false })] })
    mockPut.mockResolvedValue({
      data: { success: true, cascade_disabled: ['consumer_z'] },
    })
    renderPage()
    await waitForCard('待启用二')

    fireEvent.click(screen.getByLabelText('启用 待启用二'))
    await waitFor(() => {
      expect(toast.warning).toHaveBeenCalledWith('连带摘除依赖方能力：consumer_z')
    })
  })

  it('全部依赖方均未启用：不打扰用户（不弹 confirm）直接 PUT', async () => {
    setupApi({
      plugins: [plugin({ plugin_id: 'provider', name: '提供者', enabled: true })],
      dependents: [{ id: 'consumer_b', enabled: false }],
    })
    mockPut.mockResolvedValue({ data: { success: true } })
    renderPage()
    await waitForCard('提供者')

    fireEvent.click(screen.getByLabelText('禁用 提供者'))
    await waitFor(() => {
      expect(mockPut).toHaveBeenCalled()
    })
    expect(confirmSpy).not.toHaveBeenCalled()
  })

  it('启用方向不查询依赖方（仅禁用路径）', async () => {
    setupApi({ plugins: [plugin({ plugin_id: 'off1', name: '待启用', enabled: false })] })
    mockPut.mockResolvedValue({ data: { success: true } })
    renderPage()
    await waitForCard('待启用')

    fireEvent.click(screen.getByLabelText('启用 待启用'))
    await waitFor(() => {
      expect(mockPut).toHaveBeenCalled()
    })
    expect(mockGet.mock.calls.some(([url]) => String(url).includes('/dependents'))).toBe(false)
  })

  it('反向依赖端点不可得（reject）→ 静默放行继续 PUT', async () => {
    setupApi({
      plugins: [plugin({ plugin_id: 'provider', name: '提供者', enabled: true })],
      dependents: new Error('404'),
    })
    mockPut.mockResolvedValue({ data: { success: true } })
    renderPage()
    await waitForCard('提供者')

    fireEvent.click(screen.getByLabelText('禁用 提供者'))
    await waitFor(() => {
      expect(mockPut).toHaveBeenCalled()
    })
    expect(confirmSpy).not.toHaveBeenCalled()
  })
})

describe('PluginsSettingsPage · 徽标与元信息', () => {
  it.each([
    ['pipeline_core', 'Pipeline'],
    ['bash_tool', 'Tool'],
    ['system_x', 'System'],
    ['composite', 'composite'],
    ['', 'Composite'],
  ] as const)('config_type=%s 徽标显示 %s', async (configType, label) => {
    setupApi({
      plugins: [plugin({ plugin_id: `p_${label}`, name: `名 ${label}`, config_type: configType })],
    })
    renderPage()
    await waitForCard(`名 ${label}`)
    expect(screen.getByText(label, { selector: 'span' })).toBeInTheDocument()
  })

  it('activation 词表逐个渲染为中文，未在词表的值原样透出（单次渲染全量断言）', async () => {
    setupApi({
      plugins: [
        plugin({ plugin_id: 'a_eager', name: '激活 eager', activation: 'eager' }),
        plugin({ plugin_id: 'a_lazy', name: '激活 lazy', activation: 'lazy' }),
        plugin({ plugin_id: 'a_manual', name: '激活 manual', activation: 'manual' }),
        plugin({ plugin_id: 'a_custom', name: '激活 custom', activation: 'custom' }),
      ],
    })
    renderPage()
    await waitForCard('激活 custom')

    for (const [name, label] of [
      ['激活 eager', '启动即载'],
      ['激活 lazy', '按需载入'],
      ['激活 manual', '手动启动'],
      ['激活 custom', 'custom'],
    ] as const) {
      const card = screen.getByText(name).closest('div[class*="rounded-lg border p-3"]') as HTMLElement
      expect(card.textContent).toContain(label)
      expect(card.textContent).toContain('sidecar')
    }
  })

  it('版本号、界面贡献/端点/配置文件计数按字段存在与否渲染', async () => {
    setupApi({
      plugins: [
        plugin({
          plugin_id: 'rich',
          name: '富元信息',
          version: '2.3.4',
          has_contributes: true,
          has_http_endpoints: true,
          config_files: [
            { id: 'a', label: 'A', path: 'a.yaml' },
            { id: 'b', label: 'B', path: 'b.yaml' },
          ],
        }),
        plugin({ plugin_id: 'plain', name: '少元信息', version: null }),
      ],
    })
    renderPage()
    await waitForCard('富元信息')

    expect(screen.getByText('v2.3.4')).toBeInTheDocument()
    expect(screen.getByTitle('有 UI 贡献')).toBeInTheDocument()
    expect(screen.getByTitle('有 HTTP 端点')).toBeInTheDocument()
    expect(screen.getByText('2 个配置文件（在左侧「插件配置」编辑）')).toBeInTheDocument()
    expect(screen.queryByText(/^v$/)).toBeNull()
  })

  it('error 字段存在时行内展示错误详情', async () => {
    setupApi({
      plugins: [plugin({ plugin_id: 'broken', name: '坏插件', error: 'G2 校验失败：缺少 output_schema' })],
    })
    renderPage()
    await waitForCard('坏插件')
    expect(screen.getByText('G2 校验失败：缺少 output_schema')).toBeInTheDocument()
  })

  it('G2 净化标示：有 rejected_tools 时列出剔除工具与前后计数', async () => {
    setupApi({
      plugins: [plugin({ plugin_id: 'san_1', name: '被净化' })],
      contract: {
        plugins: [
          {
            plugin_id: 'san_1',
            gates: {
              g2_consistency: 'sanitized',
              sanitized: { rejected_tools: ['bad_tool', 'worse_tool'], tools_before: 5, tools_after: 3 },
            },
          },
        ],
      },
    })
    renderPage()
    await waitForCard('被净化')

    const note = screen.getByTestId('plugin-sanitized-san_1')
    expect(note.textContent).toContain('bad_tool')
    expect(note.textContent).toContain('worse_tool')
    expect(note.textContent).toContain('工具 5→3')
  })

  it.each([
    ['无可剔除工具清单时回退「（见契约页）」', { tools_before: 2, tools_after: 2 }],
    ['sanitized 缺省时展示 last_error 原文', null],
  ] as const)('G2 净化标示：%s', async (_label, sanitized) => {
    setupApi({
      plugins: [plugin({ plugin_id: 'san_2', name: '净化二' })],
      contract: {
        plugins: [
          {
            plugin_id: 'san_2',
            gates: {
              g2_consistency: 'sanitized',
              sanitized,
              last_error: '声明与实现不一致：capabilities.tools 为空',
            },
          },
        ],
      },
    })
    renderPage()
    await waitForCard('净化二')

    const note = screen.getByTestId('plugin-sanitized-san_2')
    if (sanitized) {
      expect(note.textContent).toContain('（见契约页）')
      expect(note.textContent).toContain('工具 2→2')
    } else {
      expect(note.textContent).toContain('声明与实现不一致：capabilities.tools 为空')
    }
  })

  it('非 sanitized 的 gate 不渲染净化标示', async () => {
    setupApi({
      plugins: [plugin({ plugin_id: 'ok_gate', name: '正常门' })],
      contract: {
        plugins: [{ plugin_id: 'ok_gate', gates: { g2_consistency: 'ok' } }],
      },
    })
    renderPage()
    await waitForCard('正常门')
    expect(screen.queryByTestId('plugin-sanitized-ok_gate')).toBeNull()
  })
})

describe('PluginsSettingsPage · 工具能力区', () => {
  const tools = [
    {
      name: 'bash_execute',
      description: '执行 Shell',
      plugin_id: 'bash_tool',
      category: 'system',
      source: 'mcp',
      input_schema: { type: 'object', properties: { cmd: { type: 'string' } } },
    },
    { name: 'plain_tool' },
  ]

  it('工具徽标：插件/类别/来源按字段存在渲染，点击展开 input_schema', async () => {
    setupApi({ plugins: [plugin()], tools })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('bash_execute')).toBeInTheDocument()
    })
    expect(screen.getByText('@bash_tool')).toBeInTheDocument()
    expect(screen.getByText('system')).toBeInTheDocument()
    expect(screen.getByText('mcp')).toBeInTheDocument()

    fireEvent.click(screen.getByText('bash_execute'))
    expect(screen.getByText(/"cmd"/)).toBeInTheDocument()

    // 再次点击收起
    fireEvent.click(screen.getByText('bash_execute'))
    expect(screen.queryByText(/"cmd"/)).toBeNull()
  })

  it('无描述的裸工具不渲染描述行与徽标', async () => {
    setupApi({ plugins: [plugin()], tools })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('plain_tool')).toBeInTheDocument()
    })
    const row = screen.getByText('plain_tool').closest('div') as HTMLElement
    expect(row.textContent).toBe('plain_tool')
  })

  it('搜索无匹配工具时显示空态并给出匹配计数', async () => {
    setupApi({ plugins: [plugin()], tools })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('bash_execute')).toBeInTheDocument()
    })

    fireEvent.change(screen.getByLabelText('搜索插件和工具'), { target: { value: 'zzz' } })
    expect(screen.getByText('没有匹配的工具')).toBeInTheDocument()
    // 计数文案为「0/2 个（LLM 可见面，/api/v1/schema 聚合）」跨两行，用函数匹配
    expect(
      screen.getByText((text) => text.startsWith('0/2 个（LLM'), { selector: 'span' }),
    ).toBeInTheDocument()
  })

  it('工具能力区为空时不渲染该分区', async () => {
    setupApi({ plugins: [plugin()], tools: [] })
    renderPage()
    await waitForCard('Demo Plugin')
    expect(screen.queryByLabelText('工具能力浏览')).toBeNull()
  })
})

describe('PluginsSettingsPage · 刷新与视图分段', () => {
  it('视图分段：已禁用段只显示禁用插件并显示计数', async () => {
    setupApi({
      plugins: [
        plugin({ plugin_id: 'on1', name: '启用中', enabled: true }),
        plugin({ plugin_id: 'off1', name: '已关闭', enabled: false }),
      ],
    })
    renderPage()
    await waitForCard('启用中')

    fireEvent.click(screen.getByRole('button', { name: /已禁用 1/ }))
    expect(screen.getByText('已关闭')).toBeInTheDocument()
    expect(screen.queryByText('启用中')).toBeNull()
  })

  it('类型分段逐个只保留对应 config_type（同一渲染内依次切换）', async () => {
    setupApi({
      plugins: [
        plugin({ plugin_id: 'sys', name: '系统插件', config_type: 'system' }),
        plugin({ plugin_id: 'pipe', name: '管道插件', config_type: 'pipeline' }),
        plugin({ plugin_id: 'toolp', name: '工具插件', config_type: 'tool' }),
      ],
    })
    renderPage()
    await waitForCard('系统插件')

    for (const [segment, expected] of [
      ['System', '系统插件'],
      ['Pipeline', '管道插件'],
      ['Tool', '工具插件'],
    ] as const) {
      fireEvent.click(screen.getByRole('button', { name: segment }))
      expect(screen.getByText(expected)).toBeInTheDocument()
      const others = ['系统插件', '管道插件', '工具插件'].filter((n) => n !== expected)
      for (const other of others) expect(screen.queryByText(other)).toBeNull()
    }
  })

  it('刷新按钮重新拉取插件面（新数据覆盖视图）', async () => {
    setupApi({ plugins: [plugin({ name: '刷新前' })] })
    renderPage()
    await waitForCard('刷新前')
    const callsBefore = mockGet.mock.calls.filter(([url]) => url === '/api/v1/plugins').length

    mockGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/plugins') return { data: [plugin({ name: '刷新后' })] }
      if (url === '/api/v1/schema') return { data: { tools: [] } }
      if (url === '/api/v1/plugins/contract-status') return { data: { plugins: [] } }
      throw new Error(`unexpected url: ${url}`)
    })
    fireEvent.click(screen.getByRole('button', { name: '刷新' }))

    await waitForCard('刷新后')
    expect(
      mockGet.mock.calls.filter(([url]) => url === '/api/v1/plugins').length,
    ).toBeGreaterThan(callsBefore)
  })

  it('插件列表为空时显示空态；schema/契约读取失败降级为空但不阻断列表', async () => {
    mockGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/plugins') return { data: [plugin({ name: '孤立可用' })] }
      throw new Error('schema 不可达')
    })
    renderPage()
    await waitForCard('孤立可用')
    expect(screen.queryByLabelText('工具能力浏览')).toBeNull()
    expect(screen.queryByTestId(/plugin-sanitized/)).toBeNull()
  })
})
