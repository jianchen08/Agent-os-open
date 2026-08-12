/**
 * PluginPageRenderer 测试（阶段2 遗留 — react-router 路由动态化）
 *
 * 验证：插件 page 可作为独立 URL 路由（可分享/刷新/浏览器前进后退）。
 * 方案：通配路由 `/p/:pageId`，PluginPageRenderer 从 useParams 取 pageId →
 * contributionRegistry.getPage → renderPageContent 渲染（widget/schema 分发）。
 *
 * 覆盖：
 * - URL /p/test-page → 调用 contributionRegistry.getPage("test-page") + 渲染 page 内容（widget 分发）
 * - URL /p/cfg-page → page 带 schema → 渲染 SchemaDriver 表单
 * - URL /p/non-existent → getPage 返回 undefined → 渲染 404 占位（不崩溃）
 * - 独立路由整页渲染（容器 data-testid="plugin-page-root"）
 */

import { render, screen } from '@testing-library/react'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { PluginPageRenderer } from '../PluginPageRenderer'

function makePage(overrides: Partial<PageDeclaration> = {}): PageDeclaration {
  return { type: 'pages', id: 'p1', space: 'workspace', ...overrides } as PageDeclaration
}

/** 在指定路径下渲染仅有 /p/:pageId 路由的 MemoryRouter */
function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/p/:pageId" element={<PluginPageRenderer />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('PluginPageRenderer — /p/:pageId 独立路由渲染', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    vi.restoreAllMocks()
  })
  afterEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    vi.restoreAllMocks()
  })

  it('URL /p/test-page → 调用 contributionRegistry.getPage("test-page") 并渲染 page widget 内容', () => {
    widgetRegistry.register(
      'test_widget',
      (props: { msg?: string }) => <div data-testid="test-widget">{props.msg}</div>,
      { name: 'test_widget', supportedSpaces: ['workspace'] },
    )
    const page = makePage({
      id: 'test-page',
      title: '测试页',
      widget: 'test_widget',
      props: { msg: 'hello-route' },
    })
    contributionRegistry.register(page)
    const spy = vi.spyOn(contributionRegistry, 'getPage')

    renderAt('/p/test-page')

    expect(spy).toHaveBeenCalledWith('test-page')
    expect(screen.getByTestId('test-widget')).toBeInTheDocument()
    expect(screen.getByText('hello-route')).toBeInTheDocument()
  })

  it('URL /p/cfg-page → page 带 schema → 渲染 SchemaDriver 表单', () => {
    const page = makePage({
      id: 'cfg-page',
      title: '配置页',
      space: 'settings',
      schema: {
        fields: [
          { name: 'model', type: 'select', label: '模型', options: [{ label: 'gpt', value: 'gpt-4' }] },
          { name: 'name', type: 'string', label: '名称', required: true },
        ],
      },
    })
    contributionRegistry.register(page)

    renderAt('/p/cfg-page')

    expect(screen.getByTestId('schema-field-model')).toBeInTheDocument()
    expect(screen.getByTestId('schema-field-name')).toBeInTheDocument()
  })

  it('URL /p/non-existent → getPage 返回 undefined → 渲染 404 占位（不崩溃）', () => {
    renderAt('/p/non-existent')

    expect(screen.getByTestId('plugin-page-not-found')).toBeInTheDocument()
  })

  it('独立路由整页渲染：根容器 data-testid="plugin-page-root"', () => {
    widgetRegistry.register(
      'full_widget',
      () => <div data-testid="full-widget">整页内容</div>,
      { name: 'full_widget', supportedSpaces: ['workspace'] },
    )
    contributionRegistry.register(
      makePage({ id: 'full-page', title: '整页', widget: 'full_widget' }),
    )

    renderAt('/p/full-page')

    expect(screen.getByTestId('plugin-page-root')).toBeInTheDocument()
    expect(screen.getByTestId('full-widget')).toBeInTheDocument()
  })
})
