/** @feature FP-T12 前端适配 | @ci: frontend-test */
/** @ci: frontend-test */
/**
 * App 根组件 — 自定义标题栏挂载门控
 *
 * Electron 主窗口渲染 TitleBar（并给 <html> 挂占位类），Web / 子浮窗不渲染。
 * router / persist 层打桩为探针路由，其余真实渲染。
 */

import { cleanup, render, screen } from '@testing-library/react'
import { type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { App } from '../App'
import type { ElectronAPI } from '../types/electron'

vi.mock('@tanstack/react-query-persist-client', () => ({
  PersistQueryClientProvider: ({ children }: { children: ReactNode }) => <>{children}</>,
}))

vi.mock('../components/extension/ExtensionHost', () => ({ ExtensionHost: () => null }))

vi.mock('../router', async () => {
  // 工厂内不用 JSX：工厂随 ../App 的模块求值被触发，早于本模块 jsx-runtime 导入初始化
  const { createMemoryRouter } = await import('react-router-dom')
  const React = await import('react')
  return {
    createRouter: () =>
      createMemoryRouter([
        {
          path: '*',
          element: React.createElement('div', { 'data-testid': 'home-probe' }, '首页探针'),
        },
      ]),
  }
})

function stubElectronApi(opts: { isChildWindow?: boolean; withControls?: boolean } = {}): void {
  const api = {
    window: {},
    isChildWindow: opts.isChildWindow ?? false,
    ...(opts.withControls === false
      ? {}
      : {
          windowControls: {
            minimize: vi.fn().mockResolvedValue(undefined),
            toggleMaximize: vi.fn().mockResolvedValue(true),
            close: vi.fn().mockResolvedValue(undefined),
            isMaximized: vi.fn().mockResolvedValue(false),
            onMaximizedChange: vi.fn(() => () => {}),
          },
        }),
  }
  window.electronAPI = api as unknown as ElectronAPI
}

beforeEach(() => {
  document.documentElement.className = ''
})

afterEach(() => {
  cleanup()
  delete (window as { electronAPI?: unknown }).electronAPI
})

describe('App — TitleBar 挂载门控', () => {
  it('Web 环境不渲染 TitleBar，路由内容正常渲染', () => {
    render(<App />)
    expect(screen.queryByTestId('custom-titlebar')).not.toBeInTheDocument()
    expect(screen.getByTestId('home-probe')).toBeInTheDocument()
  })

  it('Electron 主窗口渲染 TitleBar 并挂占位类', () => {
    stubElectronApi()
    render(<App />)
    expect(screen.getByTestId('custom-titlebar')).toBeInTheDocument()
    expect(document.documentElement.classList.contains('has-custom-titlebar')).toBe(true)
  })

  it('子浮窗不渲染 TitleBar', () => {
    stubElectronApi({ isChildWindow: true })
    render(<App />)
    expect(screen.queryByTestId('custom-titlebar')).not.toBeInTheDocument()
    expect(screen.getByTestId('home-probe')).toBeInTheDocument()
  })
})
