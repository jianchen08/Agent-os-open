/**
 * 登录页面
 *
 * 提供用户登录功能，包括：
 * - 用户名/密码表单
 * - 表单验证
 * - 登录状态处理
 * - 错误提示
 */

import { useState, useEffect } from 'react'
import { useNavigate, Link } from 'react-router-dom'
import { useAuthStore } from '../../stores/authStore'
import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'
import { ROUTES } from '../../constants/routes'

/**
 * 表单错误类型
 */
interface FormErrors {
  username?: string
  password?: string
}

/**
 * 登录页面组件
 */
export function LoginPage() {
  const navigate = useNavigate()
  const { login, isLoading, error, isAuthenticated, clearError } =
    useAuthStore()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [formErrors, setFormErrors] = useState<FormErrors>({})

  // 已认证用户自动跳转
  useEffect(() => {
    if (isAuthenticated) {
      navigate(ROUTES.HOME)
    }
  }, [isAuthenticated, navigate])

  // 清除错误
  useEffect(() => {
    return () => {
      clearError()
    }
  }, [clearError])

  /**
   * 验证表单
   */
  const validateForm = (): boolean => {
    const errors: FormErrors = {}

    if (!username.trim()) {
      errors.username = '用户名不能为空'
    }

    if (!password) {
      errors.password = '密码不能为空'
    }

    setFormErrors(errors)
    return Object.keys(errors).length === 0
  }

  /**
   * 处理登录提交
   */
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()

    if (!validateForm()) {
      return
    }

    try {
      await login(username.trim(), password)
      navigate(ROUTES.HOME)
    } catch {
      // 错误已在 store 中处理
    }
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center bg-background px-4 py-12"
      data-testid="login-page"
    >
      <div className="w-full max-w-md space-y-6">
        {/* 标题 */}
        <div className="text-center space-y-2">
          <h1 className="text-3xl font-bold text-foreground">登录</h1>
          <p className="text-muted-foreground">欢迎回来，请登录您的账号</p>
        </div>

        {/* 登录表单 */}
        <form
          onSubmit={handleSubmit}
          className="space-y-5"
          data-testid="login-form"
        >
          {/* 全局错误提示 */}
          {error && (
            <div
              className="p-3 rounded-lg bg-destructive/10 text-destructive text-sm"
              data-testid="login-error"
            >
              {error}
            </div>
          )}

          {/* 用户名输入 */}
          <div className="space-y-2">
            <label
              htmlFor="username"
              className="text-sm font-medium text-foreground block"
            >
              用户名
            </label>
            <Input
              id="username"
              type="text"
              value={username}
              onChange={e => setUsername(e.target.value)}
              placeholder="请输入用户名"
              disabled={isLoading}
              aria-invalid={!!formErrors.username}
              aria-describedby={
                formErrors.username ? 'username-error' : undefined
              }
              data-testid="login-username-input"
              className="h-10 min-h-[40px]"
            />
            {formErrors.username && (
              <p
                id="username-error"
                className="text-sm text-destructive min-h-[20px]"
                data-testid="username-error"
              >
                {formErrors.username}
              </p>
            )}
          </div>

          {/* 密码输入 */}
          <div className="space-y-2">
            <label
              htmlFor="password"
              className="text-sm font-medium text-foreground block"
            >
              密码
            </label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={e => setPassword(e.target.value)}
              placeholder="请输入密码"
              disabled={isLoading}
              aria-invalid={!!formErrors.password}
              aria-describedby={
                formErrors.password ? 'password-error' : undefined
              }
              data-testid="login-password-input"
              className="h-10 min-h-[40px]"
            />
            {formErrors.password && (
              <p
                id="password-error"
                className="text-sm text-destructive min-h-[20px]"
                data-testid="password-error"
              >
                {formErrors.password}
              </p>
            )}
          </div>

          {/* 登录按钮 */}
          <Button
            type="submit"
            className="w-full h-10 mt-2"
            disabled={isLoading}
            data-testid="login-submit-button"
          >
            {isLoading ? '登录中...' : '登录'}
          </Button>
        </form>

        {/* 注册链接 */}
        <p className="text-center text-sm text-muted-foreground pt-2">
          没有账号？{' '}
          <Link
            to={ROUTES.REGISTER}
            className="font-medium text-primary hover:underline"
            data-testid="register-link"
          >
            注册
          </Link>
        </p>
      </div>
    </div>
  )
}

export default LoginPage
