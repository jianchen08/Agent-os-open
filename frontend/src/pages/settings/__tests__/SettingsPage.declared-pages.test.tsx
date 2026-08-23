/**
 * 功能测试：settings 空间声明驱动（架构愿景功能点）
 *
 * 推演链：项目愿景（插件声明能力）→ 前端愿景（六个空间都声明驱动）→ 架构
 * （settings SpaceHost 必须消费 contributes.pages space=settings 的直接声明）→
 * 功能点（"插件声明一个 settings 页面（带 widget），它在设置导航出现，点击右侧渲染其 widget"）。
 *
 * 之前 settings 空间只消费插件 config_files（getSettingsPanels，legacy 视图），
 * 不消费直接声明的 settings 页面——这是 settings 空间未完全声明驱动的缺口。
 *
 * 本测试用真实 SettingsPage + 真实 contributionRegistry（经 loadFromSchema）+ 真实
 * widgetRegistry，断言声明页出现在导航 DOM、点击后 widget 内容渲染——端到端行为验证。
 */

import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import { renderWithProviders } from '@/test/renderWithProviders'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'

// getSchema 返回带「直接声明 settings 页」的 schema
vi.mock('@/services/api/schema', () => ({
  getSchema: vi.fn().mockResolvedValue({
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
  }),
}))

// 无关内置子页 mock，避免引入重依赖
vi.mock('@/pages/settings/ThemeSettingsPage', () => ({ ThemeSettingsPage: () => <div /> }))
vi.mock('@/pages/settings/PluginsSettingsPage', () => ({ PluginsSettingsPage: () => <div /> }))
vi.mock('@/pages/settings/PipelineSettingsPage', () => ({ PipelineSettingsPage: () => <div /> }))
vi.mock('@/pages/settings/ApiSettingsPage', () => ({ ApiSettingsPage: () => <div /> }))
vi.mock('@/pages/settings/LlmSettingsPage', () => ({ LlmSettingsPage: () => <div /> }))
vi.mock('@/components/config/PluginConfigEditor', () => ({
  PluginConfigEditor: () => <div data-testid="plugin-config-editor" />,
}))

import { SettingsPage } from '../SettingsPage'

describe('功能点：SettingsPage 渲染插件直接声明的 settings 页', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    widgetRegistry.register(
      'ext_widget',
      ((props: { label?: string }) => <div data-testid="ext-widget-rendered">{props.label}</div>) as never,
      { name: 'ext_widget', supportedSpaces: ['settings'] },
    )
  })
  afterEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })

  it('插件 contributes.pages space=settings 的页面出现在设置导航', async () => {
    renderWithProviders(<SettingsPage />)

    await waitFor(() => {
      expect(screen.getByText('扩展设置页', { exact: true })).toBeInTheDocument()
    })
  })

  it('点击声明的 settings 页 → 右侧经 PageRenderer 分发渲染其 widget 内容', async () => {
    renderWithProviders(<SettingsPage />)

    await waitFor(() => {
      expect(screen.getByText('扩展设置页', { exact: true })).toBeInTheDocument()
    })
    fireEvent.click(screen.getByText('扩展设置页', { exact: true }))

    await waitFor(() => {
      expect(screen.getByTestId('ext-widget-rendered')).toBeInTheDocument()
    })
    expect(screen.getByText('扩展设置内容可见')).toBeInTheDocument()
  })
})
