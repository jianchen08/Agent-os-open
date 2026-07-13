/**
 * 登录页面测试
 *
 * 测试内容：
 * - 页面渲染
 * - 表单验证
 * - 登录流程
 * - 错误处理
 * - 导航跳转
 */

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { LoginPage } from './LoginPage'
import { useAuthStore } from '../../stores/authStore'

// Mock authStore
vi.mock('../../stores/authStore', () => ({
  useAuthStore: vi.fn(),
}))

// Mock react-router-dom 的 useNavigate
const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  }
})

describe('LoginPage', () => {
  const mockLogin = vi.fn()
  const mockClearError = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useAuthStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      login: mockLogin,
      isLoading: false,
      error: null,
      isAuthenticated: false,
      clearError: mockClearError,
    })
  })

  const renderLoginPage = () => {
    return render(
      <MemoryRouter initialEntries={['/login']}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/" element={<div>首页</div>} />
          <Route path="/register" element={<div>注册页</div>} />
        </Routes>
      </MemoryRouter>,
    )
  }

  describe('页面渲染', () => {
    it('应该渲染登录表单', () => {
      renderLoginPage()

      expect(screen.getByRole('heading', { name: /登录/i })).toBeInTheDocument()
      expect(screen.getByLabelText(/用户名/i)).toBeInTheDocument()
      expect(screen.getByLabelText(/密码/i)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: /登录/i })).toBeInTheDocument()
    })

    it('应该显示注册链接', () => {
      renderLoginPage()

      expect(screen.getByText(/没有账号/i)).toBeInTheDocument()
      expect(screen.getByRole('link', { name: /注册/i })).toBeInTheDocument()
    })
  })

  describe('表单验证', () => {
    it('用户名为空时应该显示错误', async () => {
      const user = userEvent.setup()
      renderLoginPage()

      await user.click(screen.getByRole('button', { name: /登录/i }))

      await waitFor(() => {
        expect(screen.getByText(/用户名不能为空/i)).toBeInTheDocument()
      })
    })

    it('密码为空时应该显示错误', async () => {
      const user = userEvent.setup()
      renderLoginPage()

      await user.type(screen.getByLabelText(/用户名/i), 'testuser')
      await user.click(screen.getByRole('button', { name: /登录/i }))

      await waitFor(() => {
        expect(screen.getByText(/密码不能为空/i)).toBeInTheDocument()
      })
    })
  })

  describe('登录流程', () => {
    it('应该调用登录方法并跳转', async () => {
      const user = userEvent.setup()
      mockLogin.mockResolvedValueOnce(undefined)

      renderLoginPage()

      await user.type(screen.getByLabelText(/用户名/i), 'testuser')
      await user.type(screen.getByLabelText(/密码/i), 'password123')
      await user.click(screen.getByRole('button', { name: /登录/i }))

      await waitFor(() => {
        expect(mockLogin).toHaveBeenCalledWith('testuser', 'password123')
      })

      await waitFor(() => {
        expect(mockNavigate).toHaveBeenCalledWith('/')
      })
    })

    it('登录加载中应该禁用按钮', () => {
      ;(useAuthStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
        login: mockLogin,
        isLoading: true,
        error: null,
        isAuthenticated: false,
        clearError: mockClearError,
      })

      renderLoginPage()

      expect(screen.getByRole('button', { name: /登录中/i })).toBeDisabled()
    })
  })

  describe('错误处理', () => {
    it('应该显示登录错误信息', () => {
      ;(useAuthStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
        login: mockLogin,
        isLoading: false,
        error: '用户名或密码错误',
        isAuthenticated: false,
        clearError: mockClearError,
      })

      renderLoginPage()

      expect(screen.getByText(/用户名或密码错误/i)).toBeInTheDocument()
    })

    it('登录失败时应该显示错误', async () => {
      const user = userEvent.setup()
      mockLogin.mockRejectedValueOnce(new Error('登录失败'))

      renderLoginPage()

      await user.type(screen.getByLabelText(/用户名/i), 'testuser')
      await user.type(screen.getByLabelText(/密码/i), 'wrongpassword')
      await user.click(screen.getByRole('button', { name: /登录/i }))

      // 登录失败不应该跳转
      await waitFor(() => {
        expect(mockNavigate).not.toHaveBeenCalled()
      })
    })
  })

  describe('已认证用户', () => {
    it('已登录用户应该自动跳转到首页', () => {
      ;(useAuthStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
        login: mockLogin,
        isLoading: false,
        error: null,
        isAuthenticated: true,
        clearError: mockClearError,
      })

      renderLoginPage()

      expect(mockNavigate).toHaveBeenCalledWith('/')
    })
  })
})
