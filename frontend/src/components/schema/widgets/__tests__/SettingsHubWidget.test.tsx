/**
 * SettingsHubWidget 测试 — 工作区设置面板入口
 *
 * 背景：顶栏「设置」打开的是 SettingsHubWidget（工作区面板），而非全屏路由页
 * SettingsPage。此前 KERNEL_NAV 只有「主题」「插件注册表」，缺少「管道配置」入口，
 * 用户反馈设置页里没有管道配置页面入口。
 *
 * 验证内容（可观察行为）：
 * - AC-1: 内核设置分组出现「管道配置」入口
 * - AC-2: 点击「管道配置」→ 右侧渲染 PipelineSettingsPage（embedded 模式）
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

// ── Mock 外部依赖 ──
vi.mock('@/services/api/schema', () => ({
  getSchema: vi.fn().mockResolvedValue({}),
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

import { SettingsHubWidget } from '../SettingsHubWidget'

describe('SettingsHubWidget — 管道配置入口', () => {
  it('AC-1: 内核设置分组出现「管道配置」入口', async () => {
    render(<SettingsHubWidget />)

    await waitFor(() => {
      expect(screen.getByText('内核设置')).toBeInTheDocument()
    })
    expect(screen.getByText('管道配置')).toBeInTheDocument()
  })

  it('AC-2: 点击「管道配置」→ 渲染 PipelineSettingsPage（embedded）', async () => {
    render(<SettingsHubWidget />)

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
})
