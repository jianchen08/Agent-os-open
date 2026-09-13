/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * LazyRoute 路由级错误边界测试（T9b-E11）
 *
 * 懒加载路由（AdminPage/MemoryPage/Debug* 等）的 chunk 拉取失败或渲染抛错，
 * 由路由级 ErrorBoundary 接管：降级 UI 只替换该路由内容，不炸穿 App.tsx
 * 顶层边界（顶层一旦接管，整个 RouterProvider 被卸载，导航彻底瘫痪）。
 *
 * 行为断言：
 * - 子组件渲染抛错 → 渲染降级 UI（错误文案可见），不向外冒泡
 * - 子组件正常 → 内容照常渲染（边界透明）
 */
import { render, screen } from '@testing-library/react'
import React from 'react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
// 断掉 router.tsx 静态依赖图的重量链（Sidebar/MessageItem → @lobehub/ui →
// fluent-emoji，vitest 无法解析其 ESM 目录导入）——被测对象是 LazyRoute 边界
// 行为，UI 库不在渲染路径上。
vi.mock('@lobehub/ui', () => ({}))
vi.mock('@/components/layout/Sidebar', () => ({ Sidebar: () => null }))
import { LazyRoute } from '../router'
import { useAuthStore } from '../stores/authStore'

/** 渲染即抛错的子组件（模拟 lazy chunk 加载失败/渲染崩溃） */
function ThrowingChild(): React.ReactElement {
  throw new Error('chunk load failed: /assets/admin-legacy.js')
}

function OkChild(): React.ReactElement {
  return <div data-testid="route-content">路由内容</div>
}

describe('LazyRoute — 路由级 ErrorBoundary（E11）', () => {
  beforeEach(() => {
    // LazyRoute 内层是 ProtectedRoute：聚焦边界行为，前置已认证态跳过守卫
    useAuthStore.setState({ isInitializing: false, isAuthenticated: true })
  })

  it('子组件抛错 → 渲染降级 UI，错误不炸穿调用方', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() =>
      render(
        <MemoryRouter>
          <div data-testid="app-shell">
            <LazyRoute>
              <ThrowingChild />
            </LazyRoute>
          </div>
        </MemoryRouter>,
      ),
    ).not.toThrow()
    // 降级 UI 可见（ErrorBoundary 复用件文案）
    expect(screen.getByText('⚠️ 出错了')).toBeInTheDocument()
    // 外层 shell 未被卸载（故障隔离在路由内容层）
    expect(screen.getByTestId('app-shell')).toBeInTheDocument()
    spy.mockRestore()
  })

  it('子组件正常 → 边界透明，内容照常渲染', () => {
    render(
      <MemoryRouter>
        <LazyRoute>
          <OkChild />
        </LazyRoute>
      </MemoryRouter>,
    )
    expect(screen.getByTestId('route-content')).toBeInTheDocument()
    expect(screen.queryByText('⚠️ 出错了')).not.toBeInTheDocument()
  })
})
