/** @feature FP-T12 聊天浮层装配契约 | @ci: frontend-test */

/**
 * ChatFloatingLayer · 通用悬浮挂载槽测试（2026-10-24：位置按插件声明定位；
 * 2026-11-17：同锚点归组并排——宿主按钮与插件挂件共用一行，互不覆盖）。
 *
 * 断行为：anchor 声明渲染并定位、无 anchor 零渲染、offset 生效、同锚点并排。
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
    // 响应化契约（2026-11-22 迟到 schema 根修）：组件订阅 registry 变更，
    // mock 提供空订阅对（subscribe 返回退订函数、getVersion 恒 0）
    subscribe: () => () => {},
    getVersion: () => 0,
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
    const group = container.querySelector(
      '[data-testid="chat-floating-group-top-right"]',
    ) as HTMLElement
    expect(group).not.toBeNull()
    // top 锚点按所在 DOM 坐标上下文避让：语义=挂件距安全区底缘至少 y px。
    // 几何契约：containerTop + top >= safeTop + y
    //   => top = y + max(0px, safeTop - containerTop)
    // --floating-container-top 由所在容器声明（缺省 0=包含块顶即视口顶，当前
    // 全屏沉浸链成立）；表达式同时在场「抵扣项」与「0px 钳位」——容器已垫层时
    // 不双倍偏移，safeTop < containerTop 时负值不倒吸。
    expect(group.style.top).toBe(
      'calc(12px + max(0px, var(--safe-area-top, 0px) - var(--floating-container-top, 0px)))',
    )
    expect(group.style.right).toBe('12px')
    expect(screen.getByTestId('webview-stub').getAttribute('data-widget-id')).toBe('todo_badge')
  })

  it('top 锚点自定义 offset 叠安全区（声明 y 与安全区相加，两组取值验证）', () => {
    registryMock.pages = [
      {
        type: 'pages',
        id: 'a',
        pluginId: 'p1',
        space: 'floating',
        slot: 'panel',
        props: { anchor: 'top-right', offset: { x: 12, y: 8 } },
      },
      {
        type: 'pages',
        id: 'b',
        pluginId: 'p2',
        space: 'floating',
        slot: 'panel',
        props: { anchor: 'top-left', offset: { x: 16, y: 20 } },
      },
    ]
    const { container } = render(<ChatFloatingLayer />)
    const topRight = container.querySelector(
      '[data-testid="chat-floating-group-top-right"]',
    ) as HTMLElement
    const topLeft = container.querySelector(
      '[data-testid="chat-floating-group-top-left"]',
    ) as HTMLElement
    expect(topRight.style.top).toBe(
      'calc(8px + max(0px, var(--safe-area-top, 0px) - var(--floating-container-top, 0px)))',
    )
    expect(topLeft.style.top).toBe(
      'calc(20px + max(0px, var(--safe-area-top, 0px) - var(--floating-container-top, 0px)))',
    )
  })

  it('bottom 锚点不叠安全区顶距（底部基准不受 --safe-area-top 影响）', () => {
    registryMock.pages = [
      {
        type: 'pages',
        id: 'a',
        pluginId: 'p1',
        space: 'floating',
        slot: 'panel',
        props: { anchor: 'bottom-left', offset: { x: 24, y: 6 } },
      },
    ]
    const { container } = render(<ChatFloatingLayer />)
    const group = container.querySelector(
      '[data-testid="chat-floating-group-bottom-left"]',
    ) as HTMLElement
    expect(group.style.bottom).toBe('6px')
    expect(group.style.top).toBe('')
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
    const group = container.querySelector(
      '[data-testid="chat-floating-group-bottom-left"]',
    ) as HTMLElement
    expect(group.style.bottom).toBe('6px')
    expect(group.style.left).toBe('24px')
  })

  it('无声明零渲染（不产生幽灵层）', () => {
    const { container } = render(<ChatFloatingLayer />)
    expect(container.querySelector('[data-testid="chat-floating-layer"]')).toBeNull()
  })

  it('宿主控件与同锚点插件挂件并排：宿主在前、挂件在后，同处一行', () => {
    registryMock.pages = [
      {
        type: 'pages',
        id: 'todo_badge',
        pluginId: 'pipeline_todo_track',
        space: 'floating',
        slot: 'panel',
        props: { anchor: 'top-right', pluginId: 'pipeline_todo_track', widgetId: 'todo_badge' },
      },
    ]
    const { container } = render(
      <ChatFloatingLayer>
        <button type="button" data-testid="host-btn">
          现场
        </button>
      </ChatFloatingLayer>,
    )
    const group = container.querySelector(
      '[data-testid="chat-floating-group-top-right"]',
    ) as HTMLElement
    expect(group).not.toBeNull()
    const children = [...group.children]
    // 宿主控件排首位（右锚点行 = 从右往左增长，首位即挂件左侧，与待办角标并列）
    expect(children[0].getAttribute('data-testid')).toBe('host-btn')
    expect(children[1].getAttribute('data-testid')).toBe('chat-floating-item')
  })

  it('仅有宿主控件、该锚点无插件声明：仍渲染一行（按钮不丢）', () => {
    const { container } = render(
      <ChatFloatingLayer>
        <button type="button" data-testid="host-btn">
          现场
        </button>
      </ChatFloatingLayer>,
    )
    const group = container.querySelector(
      '[data-testid="chat-floating-group-top-right"]',
    ) as HTMLElement
    expect(group).not.toBeNull()
    expect(screen.getByTestId('host-btn')).toBeTruthy()
    expect(container.querySelectorAll('[data-testid="chat-floating-item"]').length).toBe(0)
  })
})
