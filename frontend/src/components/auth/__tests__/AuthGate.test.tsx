// @feature: FP-0.2.四 认证链(AuthGate 模态登录门) | @ci: frontend-test
/**
 * AuthGate 认证闸 + LoginModal 认证闸档测试（ADR 2026-09-28）
 *
 * - AuthGate：渲染背景占位 + 登录模态；注册入口指向注册页（整页登录版
 *   功能并入）。
 * - LoginModal dismissible=false（认证闸档）：无关闭钮，主动关闭请求不回调
 *   onClose；登录成功仍自动收口（由 ProtectedRoute 放行目标页）。
 * - LoginModal 默认档（侧边栏切换账号）：可关闭，关闭回调触发、表单重置。
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const authState = vi.hoisted(() => ({
  login: vi.fn(),
  isLoading: false,
  error: null as string | null,
  isAuthenticated: false,
  clearError: vi.fn(),
}))

vi.mock('@/stores/authStore', () => ({
  useAuthStore: () => authState,
}))

import { AuthGate } from '@/components/auth/AuthGate'
import { LoginModal } from '@/components/auth/LoginModal'

beforeEach(() => {
  vi.clearAllMocks()
  authState.isLoading = false
  authState.error = null
  authState.isAuthenticated = false
  authState.login.mockResolvedValue(undefined)
})

function renderWithRoutes(ui: React.ReactElement): void {
  render(
    <MemoryRouter initialEntries={['/']}>
      <Routes>
        <Route path="/" element={ui} />
        <Route path="/register" element={<div>注册页</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('AuthGate 认证闸', () => {
  it('渲染认证闸与登录模态表单', () => {
    renderWithRoutes(<AuthGate />)
    expect(screen.getByTestId('auth-gate')).toBeInTheDocument()
    expect(screen.getByTestId('login-modal-form')).toBeInTheDocument()
    expect(screen.getByTestId('login-modal-username')).toBeInTheDocument()
    expect(screen.getByTestId('login-modal-password')).toBeInTheDocument()
  })

  it('认证闸档无关闭钮（不可主动关闭）', () => {
    renderWithRoutes(<AuthGate />)
    expect(screen.queryByText('Close')).not.toBeInTheDocument()
  })

  it('注册入口指向注册页（自整页登录版并入）', async () => {
    const user = userEvent.setup()
    renderWithRoutes(<AuthGate />)
    await user.click(screen.getByTestId('login-modal-register-link'))
    expect(screen.getByText('注册页')).toBeInTheDocument()
  })

  it('提交登录：空表单给字段错误，不发起请求', async () => {
    const user = userEvent.setup()
    renderWithRoutes(<AuthGate />)
    await user.click(screen.getByTestId('login-modal-submit'))
    expect(screen.getByText('用户名不能为空')).toBeInTheDocument()
    expect(screen.getByText('密码不能为空')).toBeInTheDocument()
    expect(authState.login).not.toHaveBeenCalled()
  })
})

describe('LoginModal dismissible 分档', () => {
  it('默认档：渲染关闭钮，主动关闭触发 onClose', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(
      <MemoryRouter>
        <LoginModal open onClose={onClose} />
      </MemoryRouter>,
    )
    expect(screen.getByText('Close')).toBeInTheDocument()
    await user.click(screen.getByText('Close'))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it('认证闸档：Esc/关闭请求不回调 onClose，表单保持可用', async () => {
    const user = userEvent.setup()
    const onClose = vi.fn()
    render(
      <MemoryRouter>
        <LoginModal open onClose={onClose} dismissible={false} />
      </MemoryRouter>,
    )
    // DialogContent 外点不关；此处直接以 Esc 请求关闭（radix onOpenChange 路径）
    await user.keyboard('{Escape}')
    expect(onClose).not.toHaveBeenCalled()
    expect(screen.getByTestId('login-modal-form')).toBeInTheDocument()
  })

  it('展示服务端错误（store error 透传）', () => {
    authState.error = '用户名或密码错误'
    render(
      <MemoryRouter>
        <LoginModal open onClose={() => {}} />
      </MemoryRouter>,
    )
    expect(screen.getByTestId('login-modal-error')).toHaveTextContent('用户名或密码错误')
  })
})
