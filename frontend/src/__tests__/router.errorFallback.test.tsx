/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * RouteErrorFallback 路由顶层错误兜底测试：
 * - 路由渲染抛 Error → 恢复卡上屏（重启前端/返回首页按钮 + 错误详情）
 * - 抛非 Error 值（string）→ String 化展示不崩
 * - 「返回首页」→ SPA 导航回 HOME，错误态清除（可用路由恢复）
 * - 「重启前端」→ 整页 reload（window.location.reload 调用断言）
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { RouteErrorFallback } from '@/router/RouteErrorFallback'
import { useAuthStore } from '../stores/authStore'

const reloadSpy = vi.fn()
Object.defineProperty(window, 'location', {
  value: { ...window.location, reload: reloadSpy },
  writable: true,
})

function ThrowingComponent({ error }: { error: unknown }): never {
  throw error
}

/** 真渲染一条抛错路由，errorElement = RouteErrorFallback */
function renderCrashingRoute(error: unknown): void {
  const router = createMemoryRouter(
    [
      {
        path: '/broken',
        element: <ThrowingComponent error={error} />,
        errorElement: <RouteErrorFallback />,
      },
      { path: '/', element: <div data-testid="home-probe">首页</div> },
    ],
    { initialEntries: ['/broken'] },
  )
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
}

afterEach(() => {
  reloadSpy.mockClear()
  window.history.replaceState(null, '', '/')
  useAuthStore.setState({
    user: null,
    token: null,
    refreshTokenValue: null,
    isAuthenticated: false,
    mustChangePassword: false,
    isInitializing: true,
  })
})

describe('RouteErrorFallback — 路由顶层错误兜底', () => {
  it('渲染抛 Error：恢复卡上屏，含重启前端/返回首页按钮与错误详情', () => {
    renderCrashingRoute(new Error('boom: undefined container'))
    expect(screen.getByTestId('route-error-fallback')).toBeInTheDocument()
    expect(screen.getByTestId('route-error-reload')).toBeInTheDocument()
    expect(screen.getByTestId('route-error-home')).toBeInTheDocument()
    expect(screen.getByTestId('route-error-message')).toHaveTextContent('boom: undefined container')
  })

  it('抛非 Error 值（string）照常渲染恢复卡，String 化展示不崩', () => {
    renderCrashingRoute('裸字符串错误')
    expect(screen.getByTestId('route-error-fallback')).toBeInTheDocument()
    expect(screen.getByTestId('route-error-message')).toHaveTextContent('裸字符串错误')
  })

  it('点击「重启前端」调用 window.location.reload（整页重置）', () => {
    renderCrashingRoute(new Error('boom'))
    fireEvent.click(screen.getByTestId('route-error-reload'))
    expect(reloadSpy).toHaveBeenCalledTimes(1)
  })

  it('点击「返回首页」SPA 导航回 HOME，错误态随路由切换清除', async () => {
    useAuthStore.setState({ isInitializing: false, isAuthenticated: true })
    renderCrashingRoute(new Error('boom'))
    fireEvent.click(screen.getByTestId('route-error-home'))
    expect(await screen.findByTestId('home-probe')).toBeInTheDocument()
    expect(screen.queryByTestId('route-error-fallback')).not.toBeInTheDocument()
  })
})
