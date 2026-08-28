/** @feature 插件设置页 VSCode 扩展面板式 | @ci: frontend-test */
/**
 * PluginsSettingsPage 组件测试（VSCode 扩展面板式重构版）
 *
 * 核心行为契约：
 * - 统一顶部搜索框：单一入口同时过滤插件（name/id/描述/类型）与
 *   工具能力（工具名/描述/所属插件）——工具区不再有自己的搜索框
 * - 视图分段（全部/System/Pipeline/Tool/已禁用）与搜索词叠加过滤
 * - 插件卡片：manifest 描述透传展示，无描述回退 plugin_id
 *
 * 测试策略：Mock 仅外部依赖（API 层 + toast），组件真实渲染。
 */

import { screen, waitFor, fireEvent } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { PluginsSettingsPage } from '../PluginsSettingsPage'

// ── Mock API 层（apiClient.get 按 URL 分流插件面/schema tools 面）──
const mockPlugins = [
  {
    plugin_id: 'monitoring_service',
    name: 'Monitoring Service',
    description: '系统指标采集与监控告警',
    config_type: 'system',
    host_type: 'sidecar',
    version: '1.0.0',
    enabled: true,
    activation: 'lazy',
    status: 'active',
    config_files: [],
    has_contributes: false,
    has_http_endpoints: true,
    error: null,
  },
  {
    plugin_id: 'bash_tool',
    name: 'Bash Execute Tool',
    description: '执行 Shell 命令',
    config_type: 'tool',
    host_type: 'sidecar',
    version: '1.2.0',
    enabled: true,
    activation: 'lazy',
    status: 'active',
    config_files: [],
    has_contributes: false,
    has_http_endpoints: false,
    error: null,
  },
  {
    plugin_id: 'context_build',
    name: 'Context Build',
    description: null,
    config_type: 'pipeline',
    host_type: 'sidecar',
    version: '1.0.0',
    enabled: false,
    activation: 'lazy',
    status: 'disabled',
    config_files: [],
    has_contributes: false,
    has_http_endpoints: false,
    error: null,
  },
]
const mockTools = [
  { name: 'bash_execute', description: '执行 Shell 命令', plugin_id: 'bash_tool', category: 'system', source: 'mcp' },
  { name: 'resource_search', description: '搜索系统内资源', plugin_id: 'resource_search_tool', category: 'system', source: 'mcp' },
  { name: 'metrics_admin.status', description: '插件状态查询', plugin_id: 'metrics_admin', category: 'system', source: 'mcp' },
]

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

function setupApi(contract: unknown = { plugins: [] }) {
  mockGet.mockImplementation(async (url: string) => {
    if (url === '/api/v1/plugins') return { data: mockPlugins }
    if (url === '/api/v1/schema') return { data: { tools: mockTools } }
    if (url === '/api/v1/plugins/contract-status') return { data: contract }
    throw new Error(`unexpected url: ${url}`)
  })
}

function renderPage() {
  return renderWithProviders(<PluginsSettingsPage />)
}

/** 修改统一搜索框 */
function typeSearch(text: string) {
  fireEvent.change(screen.getByLabelText('搜索插件和工具'), { target: { value: text } })
}

beforeEach(() => {
  vi.clearAllMocks()
  setupApi()
})

describe('PluginsSettingsPage · 默认渲染', () => {
  it('显示全部插件与启用计数', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    expect(screen.getByText('Bash Execute Tool')).toBeInTheDocument()
    expect(screen.getByText('Context Build')).toBeInTheDocument()
    expect(screen.getByTestId('plugins-enabled-count')).toHaveTextContent('2/3 启用')
  })

  it('工具能力区显示全部工具', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('bash_execute')).toBeInTheDocument()
    })
    expect(screen.getByText('resource_search')).toBeInTheDocument()
    expect(screen.getByText('metrics_admin.status')).toBeInTheDocument()
  })

  it('无描述的插件回退显示 plugin_id', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Context Build')).toBeInTheDocument()
    })
    // description 为 null → 描述行显示 plugin_id
    expect(screen.getAllByText('context_build').length).toBeGreaterThan(0)
  })
})

describe('PluginsSettingsPage · 统一搜索（插件面）', () => {
  it('按名称片段过滤插件', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    typeSearch('monitor')
    expect(screen.queryByText('Bash Execute Tool')).not.toBeInTheDocument()
    expect(screen.queryByText('Context Build')).not.toBeInTheDocument()
    expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
  })

  it('按 plugin_id 过滤插件', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Bash Execute Tool')).toBeInTheDocument()
    })
    typeSearch('bash_tool')
    expect(screen.getByText('Bash Execute Tool')).toBeInTheDocument()
    expect(screen.queryByText('Monitoring Service')).not.toBeInTheDocument()
  })

  it('按中文描述过滤插件', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    typeSearch('监控告警')
    expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    expect(screen.queryByText('Bash Execute Tool')).not.toBeInTheDocument()
  })

  it('无匹配时显示空态', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    typeSearch('不存在的关键词xyz')
    expect(screen.getByText('没有匹配的插件')).toBeInTheDocument()
  })

  it('清空按钮恢复全部插件', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    typeSearch('monitor')
    fireEvent.click(screen.getByLabelText('清空搜索'))
    expect(screen.getByText('Bash Execute Tool')).toBeInTheDocument()
    expect(screen.getByText('Context Build')).toBeInTheDocument()
  })
})

describe('PluginsSettingsPage · 统一搜索（工具面，同一搜索框）', () => {
  it('按工具名过滤工具能力区', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('bash_execute')).toBeInTheDocument()
    })
    typeSearch('bash_execute')
    expect(screen.getByText('bash_execute')).toBeInTheDocument()
    expect(screen.queryByText('resource_search')).not.toBeInTheDocument()
    expect(screen.queryByText('metrics_admin.status')).not.toBeInTheDocument()
  })

  it('按中文描述过滤工具能力区并显示命中计数', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('bash_execute')).toBeInTheDocument()
    })
    typeSearch('插件状态')
    expect(screen.getByText('metrics_admin.status')).toBeInTheDocument()
    expect(screen.queryByText('bash_execute')).not.toBeInTheDocument()
    // 计数显示 命中数/总数
    expect(screen.getByText(/1\/3/)).toBeInTheDocument()
  })

  it('同一关键词同时命中插件卡片与工具条目', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('bash_execute')).toBeInTheDocument()
    })
    // bash_tool 是插件 id；bash_execute 工具的 plugin_id 也是 bash_tool
    typeSearch('bash_tool')
    expect(screen.getByText('Bash Execute Tool')).toBeInTheDocument()
    expect(screen.getByText('bash_execute')).toBeInTheDocument()
    expect(screen.queryByText('Monitoring Service')).not.toBeInTheDocument()
  })
})

describe('PluginsSettingsPage · 视图分段（tab 过滤）', () => {
  it('System 分段只显示 system 插件', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: 'System' }))
    expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    expect(screen.queryByText('Bash Execute Tool')).not.toBeInTheDocument()
    expect(screen.queryByText('Context Build')).not.toBeInTheDocument()
  })

  it('已禁用分段只显示禁用插件', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: /已禁用 1/ }))
    expect(screen.getByText('Context Build')).toBeInTheDocument()
    expect(screen.queryByText('Monitoring Service')).not.toBeInTheDocument()
  })

  it('分段与搜索词叠加：交集为空显示空态', async () => {
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByRole('button', { name: 'System' }))
    typeSearch('bash')
    expect(screen.getByText('没有匹配的插件')).toBeInTheDocument()
  })
})

describe('PluginsSettingsPage · G2 净化标示（ADR 2026-08-28）', () => {
  it('sanitized 插件行内标示"已净化/工具被剔除"及被剔除工具与原因', async () => {
    setupApi({
      plugins: [
        {
          plugin_id: 'bash_tool',
          enabled: true,
          gates: {
            g2_consistency: 'sanitized',
            rejected_tools: ['bash_execute'],
            last_error: '声明与实现不一致，剔除工具（需修改插件）: bash_execute',
            sanitized: {
              rejected_tools: ['bash_execute'],
              tools_before: 1,
              tools_after: 0,
              reason: 'G2 声明↔实现一致性复核失败，剔除工具后按净化 manifest 重注册',
              sanitized_ts: 1756339200000,
            },
          },
        },
      ],
    })
    renderPage()
    await waitFor(() => {
      expect(screen.getByTestId('plugin-sanitized-bash_tool')).toBeInTheDocument()
    })
    const badge = screen.getByTestId('plugin-sanitized-bash_tool')
    expect(badge).toHaveTextContent('已净化/工具被剔除')
    expect(badge).toHaveTextContent('bash_execute')
    expect(badge).toHaveTextContent('1→0')
  })

  it('契约状态正常的插件不显示净化标示', async () => {
    setupApi({
      plugins: [{ plugin_id: 'monitoring_service', gates: { g2_consistency: 'ok' } }],
    })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('plugin-sanitized-monitoring_service')).not.toBeInTheDocument()
  })

  it('契约状态端点不可用 → 列表照常渲染（标示降级为不显示）', async () => {
    mockGet.mockImplementation(async (url: string) => {
      if (url === '/api/v1/plugins') return { data: mockPlugins }
      if (url === '/api/v1/schema') return { data: { tools: mockTools } }
      throw new Error('contract-status 404')
    })
    renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    expect(screen.queryByText('已净化/工具被剔除')).not.toBeInTheDocument()
  })
})
