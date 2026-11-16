/** @feature FP-T12 聊天浮层装配契约 | @ci: frontend-test */
/**
 * ChatFloatingLayer · 通用悬浮挂载槽测试（2026-10-24：位置按插件声明定位）。
 * 断行为：anchor 声明渲染并定位、无 anchor 零渲染、offset 生效。
 */
import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { ChatFloatingLayer } from '../ChatFloatingLayer'

const registryMock = vi.hoisted(() => ({
  pages: [] as Array<Record<string, unknown>>,
}))

vi.mock('@/services/schema/ContributionRegistry', () => ({
  contributionRegistry: {
    getPagesBySpace: (space: string) =>
      space === 'floating' ? registryMock.pages : [],
  },
}))

vi.mock('@/components/schema/widgets/WebviewWidget', () => ({
  WebviewWidget: (props: { widgetId: string; title?: string }) => (
    <div data-testid="webview-stub" data-widget-id={props.widgetId} />
  ),
}))

describe('ChatFloatingLayer', () => {
  beforeEach(() => {
    registryMock.pages = []
  })

  it('带 anchor 的 floating/panel 声明渲染并按 anchor 定位', () => {
    registryMock.pages = [
      {
        type: 'pages',
        id: 'todo_badge',
        pluginId: 'pipeline_todo_track',
        space: 'floating',
        slot: 'panel',
        title: '待办角标',
        props: { anchor: 'top-right', pluginId: 'pipeline_todo_track', widgetId: 'todo_badge' },
      },
    ]
    const { container } = render(<ChatFloatingLayer />)
    const item = container.querySelector('[data-testid="chat-floating-item"]') as HTMLElement
    expect(item).not.toBeNull()
    expect(item.style.top).toBe('12px')
    expect(item.style.right).toBe('12px')
    expect(screen.getByTestId('webview-stub').getAttribute('data-widget-id')).toBe('todo_badge')
  })

  it('offset 声明生效（非 panel 槽/无 anchor 声明零渲染）', () => {
    registryMock.pages = [
      {
        type: 'pages',
        id: 'a',
        pluginId: 'p1',
        space: 'floating',
        slot: 'panel',
        props: { anchor: 'bottom-left', offset: { x: 24, y: 6 } },
      },
      {
        type: 'pages',
        id: 'b',
        pluginId: 'p2',
        space: 'floating',
        slot: 'overlay',
        props: { anchor: 'top-right' },
      },
      { type: 'pages', id: 'c', pluginId: 'p3', space: 'floating', slot: 'panel', props: {} },
    ]
    const { container } = render(<ChatFloatingLayer />)
    const items = container.querySelectorAll('[data-testid="chat-floating-item"]')
    expect(items.length).toBe(1)
    const el = items[0] as HTMLElement
    expect(el.style.bottom).toBe('6px')
    expect(el.style.left).toBe('24px')
  })

  it('无声明零渲染（不产生幽灵层）', () => {
    const { container } = render(<ChatFloatingLayer />)
    expect(container.querySelector('[data-testid="chat-floating-layer"]')).toBeNull()
  })
})
