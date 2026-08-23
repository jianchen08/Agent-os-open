/**
 * PageRenderer 测试（阶段2 前端部分B — 渲染侧直接消费 pages）
 *
 * 覆盖：
 * - PageRenderer 按 space 过滤分发渲染
 * - workspace page 带 widget → 渲染 widgetRegistry 对应组件（props 透传）
 * - settings page 带 schema.fields → 渲染 SchemaDriver 表单
 * - 无 widget 无 schema → 占位；dock 页 → 状态条目
 * - schemaToFields 适配（fields 数组形态 + JSON Schema properties 形态）
 * - Sidebar 迁移后：getPagesBySpace('workspace') + slot==='activity-bar'
 * - StatusBar 迁移后：getPagesBySpace('dock') + slot==='status'
 */

import { render, screen } from '@testing-library/react'
import { renderWithProviders } from '@/test/renderWithProviders'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { Sidebar } from '@/components/layout/Sidebar'
import { PluginStatusItems } from '@/components/layout/StatusItems'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { PageRenderer, renderPageContent, schemaToFields } from '../PageRenderer'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'

function makePage(overrides: Partial<PageDeclaration> = {}): PageDeclaration {
  return { type: 'pages', id: 'p1', space: 'workspace', ...overrides } as PageDeclaration
}

/** 注册一个直接声明的 page（type='pages'，无 legacyFrom） */
function registerPage(overrides: Partial<PageDeclaration>): void {
  contributionRegistry.register({ ...makePage(overrides), type: 'pages' })
}

describe('PageRenderer — 按 space 分发', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    vi.restoreAllMocks()
  })

  it('space 过滤：workspace 页渲染、settings 页不渲染（反之亦然）', () => {
    registerPage({ id: 'ws-1', title: '工作区页', space: 'workspace', slot: 'tab' })
    registerPage({ id: 'st-1', title: '设置页', space: 'settings', slot: 'nav' })

    renderWithProviders(<PageRenderer space="workspace" />)
    expect(screen.getByTestId('page-ws-1')).toBeInTheDocument()
    expect(screen.queryByTestId('page-st-1')).not.toBeInTheDocument()
  })

  it('space 过滤（传入 pages props）：settings 页渲染、workspace 页不渲染', () => {
    const pages = [
      makePage({ id: 'ws-1', title: '工作区页', space: 'workspace' }),
      makePage({ id: 'st-1', title: '设置页', space: 'settings' }),
    ]
    renderWithProviders(<PageRenderer pages={pages} space="settings" />)
    expect(screen.getByTestId('page-st-1')).toBeInTheDocument()
    expect(screen.queryByTestId('page-ws-1')).not.toBeInTheDocument()
  })

  it('workspace page 带 widget → 渲染 widgetRegistry 对应组件并透传 props', () => {
    widgetRegistry.register(
      'test_widget',
      (props: { msg?: string }) => <div data-testid="test-widget">{props.msg}</div>,
      { name: 'test_widget', supportedSpaces: ['workspace'] },
    )
    const page = makePage({ id: 'w1', title: '组件页', widget: 'test_widget', props: { msg: '你好 widget' } })
    renderWithProviders(<PageRenderer pages={[page]} />)
    expect(screen.getByTestId('test-widget')).toBeInTheDocument()
    expect(screen.getByText('你好 widget')).toBeInTheDocument()
  })

  it('widget 未注册 → 渲染占位（不崩溃）', () => {
    const page = makePage({ id: 'u1', title: '未知组件页', widget: 'no_such_widget' })
    renderWithProviders(<PageRenderer pages={[page]} />)
    expect(screen.getByTestId('page-placeholder-u1')).toBeInTheDocument()
    expect(screen.getByText('未知组件页')).toBeInTheDocument()
  })

  it('settings page 带 schema.fields → 渲染 SchemaDriver 表单', () => {
    const page = makePage({
      id: 'cfg-1',
      title: '配置页',
      space: 'settings',
      schema: {
        fields: [
          { name: 'model', type: 'select', label: '模型', options: [{ label: 'gpt', value: 'gpt-4' }] },
          { name: 'name', type: 'string', label: '名称', required: true },
        ],
      },
    })
    renderWithProviders(<PageRenderer pages={[page]} />)
    // RjsfForm（antd 主题）渲染：label 即字段存在证明，保存按钮由 SchemaDriver 透传
    expect(screen.getByText('模型')).toBeInTheDocument()
    expect(screen.getByText('名称')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /保\s*存/ })).toBeInTheDocument()
  })

  it('dock page（slot=status）→ 渲染状态条目', () => {
    const page = makePage({ id: 'd1', title: '磁盘用量', space: 'dock', slot: 'status' })
    renderWithProviders(<PageRenderer pages={[page]} />)
    expect(screen.getByTestId('dock-page-d1')).toBeInTheDocument()
    expect(screen.getByText('磁盘用量')).toBeInTheDocument()
  })

  it('无 widget 无 schema → 占位', () => {
    const page = makePage({ id: 'empty-1', title: '空页面' })
    renderWithProviders(<PageRenderer pages={[page]} />)
    expect(screen.getByTestId('page-placeholder-empty-1')).toBeInTheDocument()
  })

  it('renderPageContent 直接消费 PageDeclaration 返回 ReactNode', () => {
    const page = makePage({ id: 'direct-1', title: '直接调用', widget: '' })
    renderWithProviders(<div>{renderPageContent(page)}</div>)
    expect(screen.getByTestId('page-placeholder-direct-1')).toBeInTheDocument()
  })
})

describe('schemaToFields — PageDeclaration.schema → UIInputFormField[]', () => {
  it('fields 数组形态原样适配', () => {
    const fields = schemaToFields({
      fields: [
        { name: 'a', type: 'string', label: 'A' },
        { name: 'b', type: 'number', label: 'B', required: true },
      ],
    })
    expect(fields).toHaveLength(2)
    expect(fields[0]).toMatchObject({ name: 'a', type: 'string', label: 'A' })
    expect(fields[1]).toMatchObject({ name: 'b', type: 'number', label: 'B', required: true })
  })

  it('未知字段类型回退为 string；缺 name 的字段丢弃', () => {
    const fields = schemaToFields({
      fields: [{ name: 'x', type: 'blob', label: 'X' }, { label: '无名字段' }],
    })
    expect(fields).toHaveLength(1)
    expect(fields[0]).toMatchObject({ name: 'x', type: 'string', label: 'X' })
  })

  it('JSON Schema object/properties 形态适配（enum → select）', () => {
    const fields = schemaToFields({
      type: 'object',
      properties: {
        x: { type: 'string', title: 'X 字段' },
        n: { type: 'integer', title: '数量' },
        mode: { type: 'string', title: '模式', enum: ['fast', 'slow'] },
      },
    })
    expect(fields.map((f) => f.name)).toEqual(['x', 'n', 'mode'])
    expect(fields[0]).toMatchObject({ name: 'x', type: 'string', label: 'X 字段' })
    expect(fields[1]).toMatchObject({ name: 'n', type: 'number' })
    expect(fields[2]).toMatchObject({
      name: 'mode',
      type: 'select',
      options: [
        { label: 'fast', value: 'fast' },
        { label: 'slow', value: 'slow' },
      ],
    })
  })

  it('非对象/无 fields 返回空数组', () => {
    expect(schemaToFields({})).toEqual([])
    expect(schemaToFields({ title: '无结构' })).toEqual([])
  })
})

describe('Sidebar 迁移 — 消费 getPagesBySpace("workspace") + slot=activity-bar', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    vi.restoreAllMocks()
  })

  it('workspace/activity-bar 页渲染为侧栏插件入口；slot=tab 页不渲染', () => {
    const spy = vi.spyOn(contributionRegistry, 'getPagesBySpace')
    registerPage({ id: 'plug-a', title: '插件A', icon: '⚡', space: 'workspace', slot: 'activity-bar', order: 1 })
    registerPage({ id: 'plug-b', title: '插件B', space: 'workspace', slot: 'activity-bar', order: 2 })
    registerPage({ id: 'tab-x', title: '工作区Tab', space: 'workspace', slot: 'tab' })

    renderWithProviders(<Sidebar />)

    expect(screen.getByTestId('sidebar-menu-plugin-plug-a')).toBeInTheDocument()
    expect(screen.getByTestId('sidebar-menu-plugin-plug-b')).toBeInTheDocument()
    expect(screen.getByText('插件A')).toBeInTheDocument()
    // slot=tab 的 workspace 页不应进入侧栏
    expect(screen.queryByTestId('sidebar-menu-plugin-tab-x')).not.toBeInTheDocument()
    expect(spy).toHaveBeenCalledWith('workspace')
  })

  it('非 workspace 空间页不进入侧栏', () => {
    registerPage({ id: 'dock-x', title: 'Dock项', space: 'dock', slot: 'status' })
    registerPage({ id: 'set-x', title: '设置页', space: 'settings', slot: 'nav' })

    renderWithProviders(<Sidebar />)

    expect(screen.queryByTestId('sidebar-menu-plugin-dock-x')).not.toBeInTheDocument()
    expect(screen.queryByTestId('sidebar-menu-plugin-set-x')).not.toBeInTheDocument()
  })
})

describe('StatusBar 迁移 — 插件状态项消费 getPagesBySpace("dock") + slot=status（挂载侧栏底部）', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
    vi.restoreAllMocks()
  })

  it('dock/status 页渲染为状态条目；slot=item 页不渲染', () => {
    const spy = vi.spyOn(contributionRegistry, 'getPagesBySpace')
    registerPage({ id: 'st1', title: '我的状态', space: 'dock', slot: 'status' })
    registerPage({ id: 'it1', title: '普通条目', space: 'dock', slot: 'item' })

    renderWithProviders(<PluginStatusItems />)

    expect(screen.getByText('我的状态')).toBeInTheDocument()
    expect(screen.queryByText('普通条目')).not.toBeInTheDocument()
    expect(spy).toHaveBeenCalledWith('dock')
  })

  it('when 条件不满足的 status 页隐藏', () => {
    registerPage({ id: 'st2', title: '条件状态', space: 'dock', slot: 'status', when: 'no.such.key' })

    renderWithProviders(<PluginStatusItems />)

    expect(screen.queryByText('条件状态')).not.toBeInTheDocument()
  })
})
