/**
 * PageRenderer 弹出按钮测试（阶段5 detachable P1）
 *
 * 验证：
 * - page.detachable.popout 为真 → 渲染 data-testid="page-popout-btn" 按钮
 * - page.detachable.childWindow / desktopWidget 为真也渲染按钮
 * - 无 detachable 配置 → 不渲染按钮
 * - 点击按钮 → 调用 windowManager.openPopout(page)
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'
import React from 'react'
import { PageRenderer } from '@/components/schema/PageRenderer'
import { windowManager } from '@/services/window/WindowManager'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import type { PageDeclaration } from '@/services/schema/ContributionRegistry'

function makePage(overrides: Partial<PageDeclaration> = {}): PageDeclaration {
  return {
    type: 'pages',
    id: 'popout-page',
    title: '可弹出页',
    space: 'workspace',
    ...overrides,
  } as PageDeclaration
}

describe('PageRenderer — 弹出按钮渲染', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    useLayoutModeStore.setState({ floatingWindows: [] })
  })
  afterEach(() => {
    contributionRegistry.clear()
    vi.restoreAllMocks()
  })

  it('detachable.popout 为真 → 渲染弹出按钮', () => {
    const page = makePage({ detachable: { popout: true } })
    render(<PageRenderer pages={[page]} space="workspace" />)
    expect(screen.getByTestId('page-popout-btn')).toBeInTheDocument()
  })

  it('detachable.childWindow 为真 → 渲染弹出按钮', () => {
    const page = makePage({ id: 'cw-page', detachable: { childWindow: true } })
    render(<PageRenderer pages={[page]} space="workspace" />)
    expect(screen.getByTestId('page-popout-btn')).toBeInTheDocument()
  })

  it('detachable.desktopWidget 为真 → 渲染弹出按钮', () => {
    const page = makePage({ id: 'dw-page', detachable: { desktopWidget: true } })
    render(<PageRenderer pages={[page]} space="workspace" />)
    expect(screen.getByTestId('page-popout-btn')).toBeInTheDocument()
  })

  it('无 detachable 配置 → 不渲染弹出按钮', () => {
    const page = makePage()
    render(<PageRenderer pages={[page]} space="workspace" />)
    expect(screen.queryByTestId('page-popout-btn')).toBeNull()
  })

  it('detachable 三者全为 false → 不渲染弹出按钮', () => {
    const page = makePage({
      detachable: { popout: false, childWindow: false, desktopWidget: false },
    })
    render(<PageRenderer pages={[page]} space="workspace" />)
    expect(screen.queryByTestId('page-popout-btn')).toBeNull()
  })
})

describe('PageRenderer — 点击弹出按钮', () => {
  beforeEach(() => {
    contributionRegistry.clear()
    useLayoutModeStore.setState({ floatingWindows: [] })
  })
  afterEach(() => {
    contributionRegistry.clear()
    vi.restoreAllMocks()
  })

  it('点击按钮 → 调用 windowManager.openPopout(page)', () => {
    const spy = vi.spyOn(windowManager, 'openPopout').mockReturnValue('fake-id')
    const page = makePage({ detachable: { popout: true, defaultSize: { w: 300, h: 400 } } })
    render(<PageRenderer pages={[page]} space="workspace" />)

    fireEvent.click(screen.getByTestId('page-popout-btn'))
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith(page)
    spy.mockRestore()
  })
})
