// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** @ci: frontend-test */
/**
 * SettingsHubWidget — 内核设置页（后端连接/通知/关于）注册契约：
 * - 三项出现在「内核设置」导航（KERNEL_NAV_ITEMS 同源）
 * - 点击导航项 → 右侧渲染对应页面（embedded 模式透传）
 */
import { fireEvent, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { KERNEL_NAV_ITEMS } from '@/services/settingsKernelNav'
import { renderWithProviders } from '@/test/renderWithProviders'

const mockGetSchema = vi.hoisted(() => vi.fn().mockResolvedValue({}))
vi.mock('@/services/api/schema', () => ({
  getSchema: mockGetSchema,
}))
vi.mock('@/pages/settings/ThemeSettingsPage', () => ({
  ThemeSettingsPage: () => <div data-testid="theme-page" />,
}))
vi.mock('@/pages/settings/PipelineSettingsPage', () => ({
  PipelineSettingsPage: () => <div data-testid="pipeline-page" />,
}))
vi.mock('@/pages/settings/PluginsSettingsPage', () => ({
  PluginsSettingsPage: () => <div data-testid="plugins-page" />,
}))
vi.mock('@/pages/settings/devices/BackendConnectionPage', () => ({
  BackendConnectionPage: ({ embedded }: { embedded?: boolean }) => (
    <div data-testid="backend-connection-page" data-embedded={embedded ? 'true' : 'false'} />
  ),
}))
vi.mock('@/pages/settings/NotificationSettingsPage', () => ({
  NotificationSettingsPage: ({ embedded }: { embedded?: boolean }) => (
    <div data-testid="notification-page" data-embedded={embedded ? 'true' : 'false'} />
  ),
}))
vi.mock('@/pages/settings/AboutSettingsPage', () => ({
  AboutSettingsPage: ({ embedded }: { embedded?: boolean }) => (
    <div data-testid="about-page" data-embedded={embedded ? 'true' : 'false'} />
  ),
}))
vi.mock('@/components/config/PluginConfigEditor', () => ({
  PluginConfigEditor: () => <div data-testid="plugin-config-editor" />,
}))

import { SettingsHubWidget } from '../SettingsHubWidget'

describe('SettingsHubWidget — 后端连接/通知/关于 内核设置页注册', () => {
  it('KERNEL_NAV_ITEMS 含 backend-connection/notifications/about 三项（共享数据源）', () => {
    const ids = KERNEL_NAV_ITEMS.map((i) => i.id)
    expect(ids).toContain('backend-connection')
    expect(ids).toContain('notifications')
    expect(ids).toContain('about')
  })

  it.each([
    ['后端连接', 'backend-connection-page'],
    ['通知', 'notification-page'],
    ['关于', 'about-page'],
  ])('导航「%s」点击 → 渲染对应子页（embedded）', async (label, testid) => {
    renderWithProviders(<SettingsHubWidget initialActive="kernel-plugins" />)
    await waitFor(() => {
      expect(screen.getByText('内核设置')).toBeInTheDocument()
    })
    fireEvent.click(screen.getAllByText(label)[0])
    const page = await screen.findByTestId(testid)
    expect(page).toBeInTheDocument()
    expect(page.getAttribute('data-embedded')).toBe('true')
  })
})
