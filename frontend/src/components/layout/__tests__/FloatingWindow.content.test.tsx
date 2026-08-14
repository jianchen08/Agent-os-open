/**
 * 悬浮窗内容渲染测试（阶段5 detachable P1）
 *
 * 验证 renderFloatingWindowContent（FiveSpaceLayout 传给 FloatingWindowManager
 * 的 renderContent 实现）：
 * - win 带 pageId 且 page 已注册 → 调 renderPageContent（显示 page 内容/标题）
 * - win 无 page 但 component 在 widgetRegistry → 渲染该 widget（兼容旧实例）
 * - 既无 page 也无 widget → 显示占位（不崩溃）
 *
 * 备注：FloatingWindowManager 组件本身接收 renderContent 为 prop（仅透传），
 * 这里直接测试内容解析函数，确保 page→widget/schema 分发链路接通。
 */

import { render } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { renderFloatingWindowContent } from '@/components/layout/FloatingWindowManager'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import type { FloatingWindowInstance } from '@/types/layout'

function makeWindow(overrides: Partial<FloatingWindowInstance> = {}): FloatingWindowInstance {
  return {
    id: 'p1-popout-1',
    title: '原始标题',
    component: 'unknown-widget',
    position: { x: 0, y: 0 },
    size: { width: 320, height: 480 },
    zIndex: 1000,
    isMinimized: false,
    isMaximized: false,
    ...overrides,
  }
}

describe('renderFloatingWindowContent — page 分发', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })

  afterEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })

  it('win.props.pageId 指向已注册 page → 渲染 page 内容', () => {
    contributionRegistry.register({
      type: 'pages',
      id: 'p1',
      title: '页面P1',
      space: 'workspace',
    })
    const win = makeWindow({ props: { pageId: 'p1' } })

    const { getByText } = render(<>{renderFloatingWindowContent(win)}</>)
    // renderPageContent 对无 widget/schema 的 page 走 PagePlaceholder，显示 title
    expect(getByText('页面P1')).toBeInTheDocument()
  })

  it('win.id 含 popout 标记时也能解析出 pageId', () => {
    contributionRegistry.register({
      type: 'pages',
      id: 'p2',
      title: '页面P2',
      space: 'workspace',
    })
    // 不给 props.pageId，靠 id 拆分
    const win = makeWindow({ id: 'p2-popout-1700000000000', props: undefined })

    const { getByText } = render(<>{renderFloatingWindowContent(win)}</>)
    expect(getByText('页面P2')).toBeInTheDocument()
  })

  it('page 带 widget → 渲染已注册 widget', () => {
    widgetRegistry.register('my-widget', () => React.createElement('div', null, 'WIDGET_RENDERED'), {
      name: 'my-widget',
    })
    contributionRegistry.register({
      type: 'pages',
      id: 'p3',
      title: '页面P3',
      space: 'workspace',
      widget: 'my-widget',
    })
    const win = makeWindow({ props: { pageId: 'p3' } })

    const { getByText } = render(<>{renderFloatingWindowContent(win)}</>)
    expect(getByText('WIDGET_RENDERED')).toBeInTheDocument()
  })
})

describe('renderFloatingWindowContent — widget 回退（兼容旧实例）', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })
  afterEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })

  it('无 page 但 win.component 在 widgetRegistry → 渲染该 widget', () => {
    widgetRegistry.register('legacy-widget', () => React.createElement('span', null, 'LEGACY'), {
      name: 'legacy',
    })
    const win = makeWindow({ component: 'legacy-widget', props: undefined })

    const { getByText } = render(<>{renderFloatingWindowContent(win)}</>)
    expect(getByText('LEGACY')).toBeInTheDocument()
  })
})

describe('renderFloatingWindowContent — 兜底占位', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })
  afterEach(() => {
    contributionRegistry.clear()
    widgetRegistry.clear()
  })

  it('既无 page 也无 widget → 显示占位且不崩溃', () => {
    const win = makeWindow({ component: 'nothing', props: undefined })
    const { container } = render(<>{renderFloatingWindowContent(win)}</>)
    expect(container).toBeTruthy()
    // 占位应有内容渲染出来（非空 fragment）
    expect(container.textContent?.trim().length).toBeGreaterThan(0)
  })
})
