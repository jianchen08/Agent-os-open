/** @feature FP-T12 前端槽位扩展机制 | @ci: frontend-test */
/**
 * SlotHost 组件测试（设计真值 = docs/working/DSH生态嫁接通用机制设计_20261104.md §4.4）
 *
 * 契约：
 * - list 槽锚点渲染：条目按 SlotRegistry order 序渲染进宿主容器
 * - when 条件过滤（渲染侧求值；不满足不渲染）
 * - 防错位契约：声明面无几何字段——宿主容器承载边界盒（overflow 约束），
 *   每条目独立 ErrorBoundary（单条目炸只空一格不拖塌布局，相邻条目存活）
 * - component 通道：widgetRegistry 解析（双源），声明 props 下发
 * - webview 通道：WebviewWidget 沙箱承载（pluginId/htmlPath 随条目）
 * - 未注册 widget 的条目不静默空白（防御性占位可见）
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { slotRegistry } from '@/services/slots/slotRegistry'
import { SlotHost } from '../SlotHost'

// WebviewWidget 重依赖（fetch HTML/postMessage 桥）：本测只验证通道路由，mock 成轻桩
vi.mock('@/components/schema/widgets/WebviewWidget', () => ({
  WebviewWidget: (props: Record<string, unknown>) => (
    <div data-testid="webview-stub" data-html-path={String(props.htmlPath ?? '')} />
  ),
}))

// 弹出链为窗口管理器边界：mock 成 spy（真实链在 WorkspacePanel/WindowManager 侧已覆盖）
const openPopoutMock = vi.fn()
vi.mock('@/services/window/WindowManager', () => ({
  windowManager: { openPopout: (...args: unknown[]) => openPopoutMock(...args) },
}))

function OkWidget({ marker }: { marker?: unknown }): React.ReactElement {
  return <div data-testid="slot-ok-widget">{String(marker ?? 'ok')}</div>
}

function BoomWidget(): React.ReactElement {
  throw new Error('widget exploded')
}

async function loadSlots(
  slots: Array<Record<string, unknown>>,
  pluginId = 'plug_a',
): Promise<void> {
  await slotRegistry.loadFromSchema(
    { plugin_contributes: [{ plugin_id: pluginId, contributes: { slots } }] },
    { loadWidgets: vi.fn().mockResolvedValue([]) },
  )
}

beforeEach(() => {
  widgetRegistry.clear()
  widgetRegistry.register('status_card', OkWidget, { name: 'status_card' })
  widgetRegistry.register('boom', BoomWidget, { name: 'boom' })
  slotRegistry.clear()
})

describe('SlotHost（list 槽锚点）', () => {
  it('按 order 序渲染 component 通道条目，声明 props 下发', async () => {
    await loadSlots([
      { id: 'b', slot: 'sidebar.block', channel: 'component', widget: 'status_card', order: 20, props: { marker: 'second' } },
      { id: 'a', slot: 'sidebar.block', channel: 'component', widget: 'status_card', order: 10, props: { marker: 'first' } },
    ])
    render(<SlotHost slotKey="sidebar.block" />)
    const items = screen.getAllByTestId('slot-ok-widget')
    expect(items.map((el) => el.textContent)).toEqual(['first', 'second'])
  })

  it('when 条件：不满足不渲染（渲染侧求值）', async () => {
    await loadSlots([
      { id: 'admin_only', slot: 'sidebar.block', channel: 'component', widget: 'status_card', when: "user.role == 'admin'" },
      { id: 'always', slot: 'sidebar.block', channel: 'component', widget: 'status_card' },
    ])
    const { rerender } = render(<SlotHost slotKey="sidebar.block" whenContext={{ 'user.role': 'guest' }} />)
    expect(screen.getAllByTestId('slot-ok-widget')).toHaveLength(1)
    rerender(<SlotHost slotKey="sidebar.block" whenContext={{ 'user.role': 'admin' }} />)
    expect(screen.getAllByTestId('slot-ok-widget')).toHaveLength(2)
  })

  it('每条目 ErrorBoundary：单条目炸只空一格（可见占位），相邻条目存活', async () => {
    await loadSlots([
      { id: 'ok1', slot: 'sidebar.block', channel: 'component', widget: 'status_card', order: 10 },
      { id: 'explosive', slot: 'sidebar.block', channel: 'component', widget: 'boom', order: 20 },
      { id: 'ok2', slot: 'sidebar.block', channel: 'component', widget: 'status_card', order: 30 },
    ])
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<SlotHost slotKey="sidebar.block" />)
    // 相邻条目存活
    expect(screen.getAllByTestId('slot-ok-widget')).toHaveLength(2)
    // 炸的条目可见占位（非静默空白）
    expect(screen.getByTestId('slot-entry-error')).toBeInTheDocument()
    expect(screen.getByTestId('slot-entry-error').textContent).toContain('plug_a:explosive')
    spy.mockRestore()
  })

  it('webview 通道：WebviewWidget 沙箱承载（htmlPath 随条目）', async () => {
    await loadSlots([
      { id: 'heavy', slot: 'sidebar.block', channel: 'webview', htmlPath: '/panel/index.html' },
    ])
    render(<SlotHost slotKey="sidebar.block" />)
    expect(screen.getByTestId('webview-stub').getAttribute('data-html-path')).toBe('/panel/index.html')
  })

  it('运行期防御：条目 widget 未注册（禁用竞态）→ 可见占位不空白不崩', async () => {
    await loadSlots([
      { id: 'ghost', slot: 'sidebar.block', channel: 'component', widget: 'status_card' },
    ])
    // 装载后 widget 被注销（插件热禁用竞态窗口）
    widgetRegistry.unregister('status_card')
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    render(<SlotHost slotKey="sidebar.block" />)
    expect(screen.getByTestId('slot-entry-error')).toBeInTheDocument()
    spy.mockRestore()
  })

  it('空槽渲染零节点（无声明零渲染，布局零残留）', async () => {
    render(<SlotHost slotKey="sidebar.block" />)
    expect(screen.queryByTestId('slot-host-sidebar.block')).toBeNull()
  })

  it('插槽锚点域（statusbar.item 悬置）渲染零节点（fail-closed 不认词表外声明）', async () => {
    render(<SlotHost slotKey="statusbar.item" />)
    expect(document.querySelector('[data-slot-key="statusbar.item"]')).toBeNull()
  })
})

describe('SlotHost（detachable 弹出入口）', () => {
  it('detachable 条目带弹出按钮，点击走 floating window 弹出链（defaultSize 随条目）', async () => {
    openPopoutMock.mockClear()
    await loadSlots([
      {
        id: 'pop',
        slot: 'sidebar.block',
        channel: 'component',
        widget: 'status_card',
        title: '可弹出块',
        detachable: { popout: true, defaultSize: { w: 320, h: 240 } },
      },
    ])
    render(<SlotHost slotKey="sidebar.block" />)
    fireEvent.click(screen.getByTestId('slot-entry-popout-plug_a:pop'))
    await waitFor(() => {
      expect(openPopoutMock).toHaveBeenCalledTimes(1)
    })
    const arg = openPopoutMock.mock.calls[0][0] as Record<string, unknown>
    expect(arg.title).toBe('可弹出块')
    expect((arg.detachable as Record<string, unknown>).defaultSize).toEqual({ w: 320, h: 240 })
  })
})
