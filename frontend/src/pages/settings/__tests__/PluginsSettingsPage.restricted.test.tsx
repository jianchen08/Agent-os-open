/** @feature FP-T12 插件准入分级-受限标示 | @ci: frontend-test */
/**
 * PluginsSettingsPage 准入分级受限标示（2026-09-25）
 *
 * 行为契约（禁静默降级）：
 * - restricted_capabilities 非空 → 卡片渲染"已限制"条，能力 id 翻译成用户可读标签
 * - 未声明该字段（旧内核响应）/空数组 → 不渲染条（不占位不报错）
 *
 * 测试策略：Mock 仅外部依赖（API 层），组件真实渲染。
 */

import { screen, waitFor } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { PluginsSettingsPage } from '../PluginsSettingsPage'
import { apiClientStub } from './pluginsSettingsTestUtils'

const restrictedPlugin = {
  plugin_id: 'third_party_skin',
  name: 'Third Party Skin',
  description: '第三方皮肤插件',
  config_type: 'system',
  host_type: 'sidecar',
  version: '0.1.0',
  enabled: true,
  activation: 'lazy',
  status: 'active',
  config_files: [],
  has_contributes: true,
  has_http_endpoints: true,
  restricted_capabilities: ['host_js', 'host_css'],
  error: null,
}

const cleanPlugin = {
  plugin_id: 'normal_plugin',
  name: 'Normal Plugin',
  description: null,
  config_type: 'tool',
  host_type: 'sidecar',
  version: '1.0.0',
  enabled: true,
  activation: 'lazy',
  status: 'active',
  config_files: [],
  has_contributes: false,
  has_http_endpoints: false,
  error: null,
}

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

describe('PluginsSettingsPage · 准入受限标示', () => {
  it('restricted_capabilities 非空 → 渲染已限制条并翻译能力标签', async () => {
    setupApi([restrictedPlugin])
    renderWithProviders(<PluginsSettingsPage />)
    const strip = await screen.findByTestId('plugin-restricted-third_party_skin')
    expect(strip).toHaveTextContent('已限制')
    expect(strip).toHaveTextContent('宿主脚本（皮肤 hooks）')
    expect(strip).toHaveTextContent('宿主样式注入')
  })

  it('未声明 restricted_capabilities（旧内核响应）→ 不渲染条', async () => {
    setupApi([cleanPlugin])
    renderWithProviders(<PluginsSettingsPage />)
    await waitFor(() => {
      expect(screen.getByText('Normal Plugin')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('plugin-restricted-normal_plugin')).not.toBeInTheDocument()
  })

  it('restricted_capabilities 为空数组 → 不渲染条（≥2 组区分输入）', async () => {
    setupApi([{ ...cleanPlugin, restricted_capabilities: [] }])
    renderWithProviders(<PluginsSettingsPage />)
    await waitFor(() => {
      expect(screen.getByText('Normal Plugin')).toBeInTheDocument()
    })
    expect(screen.queryByTestId('plugin-restricted-normal_plugin')).not.toBeInTheDocument()
  })
})
