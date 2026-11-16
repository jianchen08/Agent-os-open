/** @feature 插件设置页 WebView 110 兼容（R11） | @ci: frontend-test */
/**
 * PluginsSettingsPage — WebView 110（Chromium <111）防回归测试
 *
 * 根因（MuMu CDP 实测）：Tailwind v4 对 bg-[color-mix(in_srgb,var(--ds-*),N%,transparent)]
 * 生成两段式产物——@supports color-mix 门内才是半透明淡底，门外 fallback 是裸
 * var 实心色；110 不过门 → 实心 accent 底 + 同色图标 = 隐形。
 * 契约：页面渲染产物禁入 color-mix 类（110 破坏形态），半透明底/边框一律
 * rgb(var(--ds-*-rgb)/alpha) 原生 alpha 语法（Chromium 65+，无门、双端一致）。
 *
 * 测试策略：Mock 仅外部依赖（API 层 + toast），组件真实渲染（同 PluginsSettingsPage.test.tsx）。
 */

import { screen, waitFor } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { PluginsSettingsPage } from '../PluginsSettingsPage'
import { MONITORING_PLUGIN, apiClientStub } from './pluginsSettingsTestUtils'

const mockPlugins = [
  MONITORING_PLUGIN,
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
]

vi.mock('@/services/api/client', async () => ({
  default: (await import('./pluginsSettingsTestUtils')).apiClientStub,
}))

vi.mock('@/services/modules/GrowthLoop', () => ({
  refreshPluginContributions: vi.fn().mockResolvedValue(undefined),
}))

vi.mock('@/components/ui/sonner', async () => ({
  toast: (await import('./pluginsSettingsTestUtils')).toastStub,
}))

function renderPage() {
  return renderWithProviders(<PluginsSettingsPage />)
}

beforeEach(() => {
  vi.clearAllMocks()
  apiClientStub.get.mockImplementation(async (url: string) => {
    if (url === '/api/v1/plugins') return { data: mockPlugins }
    if (url === '/api/v1/schema') return { data: { tools: mockTools } }
    if (url === '/api/v1/plugins/contract-status') return { data: { plugins: [] } }
    throw new Error(`unexpected url: ${url}`)
  })
})

describe('PluginsSettingsPage · WebView 110 兼容（禁入 color-mix 破坏形态）', () => {
  it('渲染产物不含任何 color-mix 类（110 两段式实心 fallback 根因禁入）', async () => {
    const { container } = renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    expect(container.innerHTML).not.toContain('color-mix(in_srgb')
    expect(container.innerHTML).not.toContain('color-mix(')
  })

  it('三种类型分支的半透明底/边框均为 rgb(var(--ds-*-rgb)/alpha) 原生 alpha 类', async () => {
    const { container } = renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    // system（monitoring_service）→ --ds-status-info-rgb
    expect(container.innerHTML).toContain('bg-[rgb(var(--ds-status-info-rgb)/0.10)]')
    expect(container.innerHTML).toContain('border-[rgb(var(--ds-status-info-rgb)/0.35)]')
    // tool（bash_tool）→ --ds-accent-primary-rgb
    expect(container.innerHTML).toContain('bg-[rgb(var(--ds-accent-primary-rgb)/0.10)]')
    expect(container.innerHTML).toContain('border-[rgb(var(--ds-accent-primary-rgb)/0.35)]')
    // pipeline（context_build，禁用列表可见）→ --ds-accent-ai-rgb
    expect(container.innerHTML).toContain('bg-[rgb(var(--ds-accent-ai-rgb)/0.12)]')
    expect(container.innerHTML).toContain('border-[rgb(var(--ds-accent-ai-rgb)/0.35)]')
  })

  it('工具能力区 category 徽章走 -rgb 三元组形态', async () => {
    const { container } = renderPage()
    await waitFor(() => {
      expect(screen.getByText('bash_execute')).toBeInTheDocument()
    })
    // category 徽章（bg 12% + border 35%）→ --ds-accent-primary-rgb
    expect(container.innerHTML).toContain('bg-[rgb(var(--ds-accent-primary-rgb)/0.12)]')
  })

  it('错误卡片边框走 -rgb 三元组内联形态（error 插件高亮 110 下仍可见）', async () => {
    apiClientStub.get.mockImplementation(async (url: string) => {
      if (url === '/api/v1/plugins') return { data: [{ ...mockPlugins[0], error: 'boom' }] }
      if (url === '/api/v1/schema') return { data: { tools: [] } }
      if (url === '/api/v1/plugins/contract-status') return { data: { plugins: [] } }
      throw new Error(`unexpected url: ${url}`)
    })
    const { container } = renderPage()
    await waitFor(() => {
      expect(screen.getByText('Monitoring Service')).toBeInTheDocument()
    })
    const card = screen.getByText('Monitoring Service').closest('div.rounded-lg') as HTMLElement
    expect(card).not.toBeNull()
    expect(card.style.borderColor).toContain('--ds-status-error-rgb')
    // 全页产物复核：错误场景也不得出现 color-mix 破坏形态
    expect(container.innerHTML).not.toContain('color-mix(')
  })
})
