/**
 * SettingsHubWidget 测试 — 设置中枢（设置唯一 UI）
 *
 * 背景：设置一律在工作区页签打开（SettingsHubWidget）。独立路由页 /settings
 * 已退役（2026-08-24），本组件承载全部设置入口。
 *
 * 验证内容（可观察行为）：
 * - AC-1: 内核设置分组出现「管道配置」入口
 * - AC-2: 点击「管道配置」→ 右侧渲染 PipelineSettingsPage（embedded 模式）
 * - AC-3: 插件 contributes.pages space=settings 的声明页出现在导航（插件页面组）
 * - AC-4: initialActive 深链（管道编辑器 step 节点等外部入口）直达插件配置页
 */

import { fireEvent, screen, waitFor } from '@testing-library/react'
import { renderWithProviders } from '@/test/renderWithProviders'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'

// ── Mock 外部依赖 ──
const mockGetSchema = vi.hoisted(() => vi.fn().mockResolvedValue({}))

vi.mock('@/services/api/schema', () => ({
  getSchema: mockGetSchema,
}))

vi.mock('@/pages/settings/PipelineSettingsPage', () => ({
  PipelineSettingsPage: ({ embedded }: { embedded?: boolean }) => (
    <div data-testid="pipeline-page" data-embedded={embedded ? 'true' : 'false'}>
      管道配置页
    </div>
  ),
}))

vi.mock('@/pages/settings/ThemeSettingsPage', () => ({
  ThemeSettingsPage: () => <div data-testid="theme-page" />,
}))

vi.mock('@/pages/settings/PluginsSettingsPage', () => ({
  PluginsSettingsPage: () => <div data-testid="plugins-page" />,
}))

vi.mock('@/components/config/PluginConfigEditor', () => ({
  PluginConfigEditor: ({ pluginId, fileId }: { pluginId?: string; fileId?: string }) => (
    <div data-testid="plugin-config-editor">
      {pluginId}/{fileId}
    </div>
  ),
}))

import { SettingsHubWidget } from '../SettingsHubWidget'

describe('SettingsHubWidget — 管道配置入口', () => {
  it('AC-1: 内核设置分组出现「管道配置」入口', async () => {
    renderWithProviders(<SettingsHubWidget />)

    await waitFor(() => {
      expect(screen.getByText('内核设置')).toBeInTheDocument()
    })
    expect(screen.getByText('管道配置')).toBeInTheDocument()
  })

  it('AC-2: 点击「管道配置」→ 渲染 PipelineSettingsPage（embedded）', async () => {
    renderWithProviders(<SettingsHubWidget />)

    await waitFor(() => {
      expect(screen.getByText('管道配置')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('管道配置'))

    await waitFor(() => {
      const page = screen.getByTestId('pipeline-page')
      expect(page).toBeInTheDocument()
      expect(page.getAttribute('data-embedded')).toBe('true')
    })
  })

  it('AC-4: initialActive 深链直达插件配置页（无需先点导航）', () => {
    renderWithProviders(
      <SettingsHubWidget initialActive="plugin:some-plugin:default" />,
    )

    expect(screen.getByTestId('plugin-config-editor')).toBeInTheDocument()
    expect(screen.getByTestId('plugin-config-editor')).toHaveTextContent(
      'some-plugin/default',
    )
  })
})

describe('SettingsHubWidget — 声明驱动（contributes.pages space=settings）', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    mockGetSchema.mockResolvedValue({
      plugin_contributes: [
        {
          plugin_id: 'ext-plugin',
          plugin_name: '扩展插件',
          contributes: {
            pages: [
              {
                id: 'ext-settings-page',
                title: '扩展设置页',
                space: 'settings',
                slot: 'nav',
                widget: 'ext_widget',
                props: { label: '扩展设置内容可见' },
              },
            ],
          },
        },
      ],
    })
    widgetRegistry.register(
      'ext_widget',
      ((props: { label?: string }) => (
        <div data-testid="ext-widget-rendered">{props.label}</div>
      )) as never,
      { name: 'ext_widget', supportedSpaces: ['settings'] },
    )
  })

  afterEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    mockGetSchema.mockReset()
    mockGetSchema.mockResolvedValue({})
  })

  it('AC-3: 声明的 settings 页出现在「插件页面」导航组，点击渲染其 widget', async () => {
    renderWithProviders(<SettingsHubWidget />)

    await waitFor(() => {
      expect(screen.getByText('插件页面')).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('扩展设置页', { exact: true }))

    await waitFor(() => {
      expect(screen.getByTestId('ext-widget-rendered')).toBeInTheDocument()
    })
    expect(screen.getByText('扩展设置内容可见')).toBeInTheDocument()
  })
})
