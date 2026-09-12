// @feature FP-T12 前端组件补测
/**
 * 注册页面测试
 *
 * 测试内容：
 * - 页面渲染
 * - 字段级验证（失焦触发：用户名规则/邮箱/密码/确认密码）
 * - 提交流程（校验拦截、注册成功跳转、isLoading 禁用）
 * - 全局错误展示与卸载清理
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Routes, Route } from 'react-router-dom'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { RegisterPage } from './RegisterPage'
import { useAuthStore } from '../../stores/authStore'

vi.mock('../../stores/authStore', () => ({
  useAuthStore: vi.fn(),
}))

const mockNavigate = vi.fn()
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom')
  return {
    ...actual,
    useNavigate: () => mockNavigate,
  }
})

describe('RegisterPage', () => {
  const mockRegister = vi.fn()
  const mockClearError = vi.fn()

  beforeEach(() => {
    vi.clearAllMocks()
    ;(useAuthStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
      register: mockRegister,
      isLoading: false,
      error: null,
      isAuthenticated: false,
      clearError: mockClearError,
    })
  })

  const renderRegisterPage = () => {
    return render(
      <MemoryRouter initialEntries={['/register']}>
        <Routes>
          <Route path="/register" element={<RegisterPage />} />
          <Route path="/" element={<div>首页</div>} />
          <Route path="/login" element={<div>登录页</div>} />
        </Routes>
      </MemoryRouter>,
    )
  }

  const fireEventBlur = (el: Element) => fireEvent.blur(el)

  async function fillValidForm() {
    await userEvent.type(screen.getByTestId('register-username-input'), 'alice')
    await userEvent.type(screen.getByTestId('email-input'), 'alice@example.com')
    await userEvent.type(screen.getByTestId('register-password-input'), 'secret123')
    await userEvent.type(screen.getByTestId('confirm-password-input'), 'secret123')
  }

  it('渲染注册表单与登录链接', () => {
    renderRegisterPage()

    expect(screen.getByRole('heading', { name: '注册' })).toBeInTheDocument()
    expect(screen.getByTestId('register-username-input')).toBeInTheDocument()
    expect(screen.getByTestId('email-input')).toBeInTheDocument()
    expect(screen.getByTestId('register-password-input')).toBeInTheDocument()
    expect(screen.getByTestId('confirm-password-input')).toBeInTheDocument()
    expect(screen.getByTestId('register-submit-button')).toBeInTheDocument()
    expect(screen.getByTestId('login-link')).toBeInTheDocument()
  })

  describe('字段级验证（失焦）', () => {
    it('用户名过短与非法字符分别给出对应错误', async () => {
      renderRegisterPage()

      const input = screen.getByTestId('register-username-input')
      await userEvent.type(input, 'ab')
      fireEventBlur(input)
      expect(await screen.findByTestId('register-username-error')).toHaveTextContent(
        '用户名至少3个字符',
      )

      await userEvent.clear(input)
      await userEvent.type(input, 'bad name!')
      fireEventBlur(input)
      expect(await screen.findByTestId('register-username-error')).toHaveTextContent(
        '仅支持字母',
      )
    })

    it('无效邮箱失焦报错；修正后错误消失', async () => {
      renderRegisterPage()

      const input = screen.getByTestId('email-input')
      await userEvent.type(input, 'not-an-email')
      fireEventBlur(input)
      expect(await screen.findByTestId('email-error')).toHaveTextContent('邮箱地址')

      await userEvent.clear(input)
      await userEvent.type(input, 'a@b.co')
      fireEventBlur(input)
      await waitFor(() =>
        expect(screen.queryByTestId('email-error')).not.toBeInTheDocument(),
      )
    })

    it('密码不足 6 位报错；两次密码不一致报错', async () => {
      renderRegisterPage()

      const pwd = screen.getByTestId('register-password-input')
      await userEvent.type(pwd, '12345')
      fireEventBlur(pwd)
      expect(await screen.findByTestId('register-password-error')).toHaveTextContent(
        '至少6个字符',
      )

      const confirm = screen.getByTestId('confirm-password-input')
      await userEvent.type(confirm, '123456')
      fireEventBlur(confirm)
      expect(await screen.findByTestId('confirm-password-error')).toHaveTextContent(
        '不一致',
      )
    })
  })

  describe('提交流程', () => {
    it('校验失败：不调 register、字段错误全量展示', async () => {
      renderRegisterPage()
      await userEvent.click(screen.getByTestId('register-submit-button'))

      expect(mockRegister).not.toHaveBeenCalled()
      expect(screen.getByTestId('register-username-error')).toBeInTheDocument()
      expect(screen.getByTestId('email-error')).toBeInTheDocument()
    })

    it('校验通过：register(用户名, 密码, 邮箱) 被调用', async () => {
      mockRegister.mockResolvedValue(undefined)
      renderRegisterPage()
      await fillValidForm()
      await userEvent.click(screen.getByTestId('register-submit-button'))

      await waitFor(() =>
        expect(mockRegister).toHaveBeenCalledWith('alice', 'secret123', 'alice@example.com'),
      )
    })

    it('isAuthenticated → 自动跳转首页', async () => {
      ;(useAuthStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
        register: mockRegister,
        isLoading: false,
        error: null,
        isAuthenticated: true,
        clearError: mockClearError,
      })
      renderRegisterPage()

      await waitFor(() => expect(mockNavigate).toHaveBeenCalledWith('/'))
    })

    it('isLoading → 提交按钮禁用且显示注册中', () => {
      ;(useAuthStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
        register: mockRegister,
        isLoading: true,
        error: null,
        isAuthenticated: false,
        clearError: mockClearError,
      })
      renderRegisterPage()

      expect(screen.getByTestId('register-submit-button')).toBeDisabled()
      expect(screen.getByText('注册中...')).toBeInTheDocument()
    })

    it('store 错误展示；卸载时 clearError 清理', async () => {
      const { unmount } = renderRegisterPage()
      unmount()
      ;(useAuthStore as unknown as ReturnType<typeof vi.fn>).mockReturnValue({
        register: mockRegister,
        isLoading: false,
        error: '用户名已被占用',
        isAuthenticated: false,
        clearError: mockClearError,
      })
      render(
        <MemoryRouter initialEntries={['/register']}>
          <Routes>
            <Route path="/register" element={<RegisterPage />} />
          </Routes>
        </MemoryRouter>,
      )
      expect(screen.getByTestId('register-error')).toHaveTextContent('用户名已被占用')

      await waitFor(() => expect(mockClearError).toHaveBeenCalled())
    })
  })
})
