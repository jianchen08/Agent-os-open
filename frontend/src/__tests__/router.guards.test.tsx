/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * router.tsx 路由守卫与路由表测试（与 router.lazyRoute.test.tsx 互补：
 * 那里只测 LazyRoute 的 ErrorBoundary 边界行为，这里测守卫分支与 createRouter 路由表）。
 *
 * 守卫分支（经导出的 LazyRoute 观测，内部为 ProtectedRoute）：
 * - 初始化中 → 加载占位，不渲染内容也不跳登录
 * - 未认证 → 重定向登录页（开发与生产行为一致，无 dev 放行旁路）
 * - 已认证 → 内容照常渲染
 * - 首登强制改密闸拦截（任何认证态先过此闸）
 *
 * 路由表（createRouter + RouterProvider 真渲染）：
 * - HOME 主界面渲染 / 404 通配重定向回 HOME / 登录、注册页可达 / 插件 page 通配路由命中
 *
 * mock 边界：仅外部服务（api 传输层 apiClient）与 vitest 无法解析的重 UI 依赖链
 * （@lobehub/ui → fluent-emoji、Sidebar → MessageItem → @lobehub/ui，同既有测试）。
 * 路由与守卫行为全部真渲染观测。
 */
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, render, screen } from '@testing-library/react'
import React from 'react'
import { MemoryRouter, Route, Routes, RouterProvider } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'
const mockApiGet = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/client', () => {
  const client = {
    get: (...args: unknown[]) => mockApiGet(...args),
    post: vi.fn(),
    patch: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  }
  return { default: client, apiClient: client }
})
// 断掉 vitest 无法解析的 ESM 目录导入链（同 router.lazyRoute.test.tsx）
vi.mock('@lobehub/ui', () => ({}))
vi.mock('@/components/layout/Sidebar', () => ({ Sidebar: () => null }))
import { ROUTES } from '../constants/routes'
import { createRouter, LazyRoute } from '../router'
import { useAuthStore } from '../stores/authStore'

// query 钩子（sessions/agents/longTermTasks）挂载即拉数据：api 传输层 mock 为空信封
mockApiGet.mockResolvedValue({ data: { items: [], threads: [], children: [], tree: [], tasks: [] } })

function GuardProbe(): React.ReactElement {
  return <div data-testid="protected-content">受保护内容</div>
}

function LoginProbe(): React.ReactElement {
  return <div data-testid="login-probe">登录探针</div>
}

/** 在受保护路径上挂 LazyRoute，并布置 /login 探针路由观测重定向落点 */
function renderGuard(): void {
  render(
    <MemoryRouter initialEntries={['/private']}>
      <Routes>
        <Route
          path="/private"
          element={
            <LazyRoute>
              <GuardProbe />
            </LazyRoute>
          }
        />
        <Route path={ROUTES.LOGIN} element={<LoginProbe />} />
      </Routes>
    </MemoryRouter>,
  )
}

/** createRouter + RouterProvider 真渲染；起始路径非 HOME 时经 router.navigate 导航 */
async function renderAt(path: string): Promise<void> {
  const router = createRouter()
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  if (path !== ROUTES.HOME) {
    await act(async () => {
      await router.navigate(path)
    })
  }
}

afterEach(() => {
  vi.unstubAllEnvs()
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

describe('ProtectedRoute 守卫分支（经 LazyRoute 观测）', () => {
  it('初始化中：渲染加载占位，不渲染内容也不跳登录', () => {
    // store 保持默认（未认证 + 初始化中）
    renderGuard()
    expect(screen.getByText('加载中...')).toBeInTheDocument()
    expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument()
    expect(screen.queryByTestId('login-probe')).not.toBeInTheDocument()
  })

  it('未认证：重定向到登录页（开发与生产行为一致，无 dev 放行旁路）', () => {
    // vitest 环境 import.meta.env.DEV = true——守卫不再读 DEV，未认证照旧重定向
    useAuthStore.setState({ isInitializing: false })
    renderGuard()
    expect(screen.getByTestId('login-probe')).toBeInTheDocument()
    expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument()
  })

  it('已认证：受保护内容照常渲染', () => {
    useAuthStore.setState({ isInitializing: false, isAuthenticated: true })
    renderGuard()
    expect(screen.getByTestId('protected-content')).toBeInTheDocument()
  })

  it('首登强制改密闸：拦截受保护内容（先于其他分支）', () => {
    useAuthStore.setState({ isInitializing: false, isAuthenticated: true, mustChangePassword: true })
    renderGuard()
    expect(screen.getByTestId('change-password-gate')).toBeInTheDocument()
    expect(screen.queryByTestId('protected-content')).not.toBeInTheDocument()
  })
})

describe('createRouter 路由表', () => {
  it('HOME 渲染主界面（未选择会话时显示欢迎引导）', async () => {
    useAuthStore.setState({ isInitializing: false, isAuthenticated: true })
    await renderAt(ROUTES.HOME)
    expect(await screen.findByText('欢迎使用超级终端')).toBeInTheDocument()
  })

  it('未匹配路径（404 通配）重定向回 HOME', async () => {
    useAuthStore.setState({ isInitializing: false, isAuthenticated: true })
    await renderAt('/definitely/not/exist')
    expect(await screen.findByText('欢迎使用超级终端')).toBeInTheDocument()
  })

  it.each([
    { path: ROUTES.LOGIN, probe: 'login-form' },
    { path: ROUTES.REGISTER, probe: 'register-form' },
  ])('路由表 $path 渲染对应认证页', async ({ path, probe }) => {
    await renderAt(path)
    expect(await screen.findByTestId(probe)).toBeInTheDocument()
  })

  it('通配 /p/:pageId 命中插件页渲染器而非 404 重定向', async () => {
    useAuthStore.setState({ isInitializing: false, isAuthenticated: true })
    await renderAt('/p/no-such-page')
    // registry 未初始化（schema 异步拉取未完成）时渲染 loading 占位并轮询重试
    expect(await screen.findByTestId('plugin-page-loading')).toBeInTheDocument()
  })
})
