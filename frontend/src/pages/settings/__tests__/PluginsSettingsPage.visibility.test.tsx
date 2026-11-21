/** @feature FP-0.2.一 private 插件可见性 | @ci: frontend-test */
/**
 * PluginsSettingsPage private 可见性过滤（批 2 private 机制落地）
 *
 * 设计真值：ADR 2026-11-18-llm-two-tier-plugin-convergence 决策 5 —— private 是
 * 分发与可见性属性（不是能力属性）：随仓内置分发、内核同链路装载，仅不进用户
 * 插件安装/管理面。
 *
 * 行为契约：
 * - visibility=private → 插件管理列表不展示（其能力面 contributes/工具/端点不受影响，
 *   不在本页断言范围）
 * - visibility=public → 照常展示
 * - 未声明 visibility（旧内核响应）→ 照常展示（宽容读，不得误伤既有插件）
 * - 视图分段计数以过滤后列表为准（「全部 N」不含 private）
 *
 * 测试策略：Mock 仅外部依赖（API 层），组件真实渲染。
 */

import { screen, waitFor } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { PluginsSettingsPage } from '../PluginsSettingsPage'
import { apiClientStub } from './pluginsSettingsTestUtils'

function plugin(id: string, name: string, extra: Record<string, unknown> = {}) {
  return {
    plugin_id: id,
    name,
    description: `${name} 描述`,
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
    ...extra,
  }
}

const privatePlugin = plugin('llm_service', 'LLM Service', { visibility: 'private' })
const publicPlugin = plugin('public_tool', 'Public Tool', { visibility: 'public' })
const legacyPlugin = plugin('legacy_tool', 'Legacy Tool')

vi.mock('@/services/api/client', async () => ({
  default: (await import('./pluginsSettingsTestUtils')).apiClientStub,
}))

vi.mock('@/services/modules/GrowthLoop', () => ({
  refreshPluginContributions: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('@/components/ui/sonner', async () => ({
  toast: (await import('./pluginsSettingsTestUtils')).toastStub,
}))

function setupApi(plugins: unknown[]) {
  apiClientStub.get.mockImplementation(async (url: string) => {
    if (url === '/api/v1/plugins') return { data: plugins }
    if (url === '/api/v1/schema') return { data: { tools: [] } }
    if (url === '/api/v1/plugins/contract-status') return { data: { plugins: [] } }
    throw new Error(`unexpected url: ${url}`)
  })
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('PluginsSettingsPage · private 可见性过滤', () => {
  it('visibility=private 的插件不进管理列表，public 照常展示', async () => {
    setupApi([privatePlugin, publicPlugin])
    renderWithProviders(<PluginsSettingsPage />)

    await screen.findByText('Public Tool')
    expect(screen.queryByText('LLM Service')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('禁用 LLM Service')).not.toBeInTheDocument()
  })

  it('未声明 visibility（旧内核响应）→ 照常展示（宽容读）', async () => {
    setupApi([legacyPlugin, publicPlugin])
    renderWithProviders(<PluginsSettingsPage />)

    await screen.findByText('Legacy Tool')
    expect(screen.getByText('Public Tool')).toBeInTheDocument()
  })

  it('分段计数按过滤后列表（全部 N 不含 private）', async () => {
    setupApi([privatePlugin, publicPlugin, legacyPlugin])
    renderWithProviders(<PluginsSettingsPage />)

    await screen.findByText('Public Tool')
    await waitFor(() => {
      expect(screen.getByRole('button', { name: '全部 2' })).toBeInTheDocument()
    })
    expect(screen.queryByRole('button', { name: '全部 3' })).not.toBeInTheDocument()
  })
})
