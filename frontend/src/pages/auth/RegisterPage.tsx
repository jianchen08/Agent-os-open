/**
 * 注册页面
 *
 * 提供用户注册功能，包括：
 * - 用户名/邮箱/密码表单
 * - 表单验证
 * - 注册状态处理
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
  email?: string
  password?: string
  confirmPassword?: string
}

/**
 * 注册页面组件
 */
export function RegisterPage() {
  const navigate = useNavigate()
  const { register, isLoading, error, isAuthenticated, clearError } =
    useAuthStore()

  const [username, setUsername] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
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
    } else if (username.length < 3) {
      errors.username = '用户名至少3个字符'
    }

    if (!email.trim()) {
      errors.email = '邮箱不能为空'
    } else if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) {
      errors.email = '请输入有效的邮箱地址'
    }

    if (!password) {
      errors.password = '密码不能为空'
    } else if (password.length < 6) {
      errors.password = '密码至少6个字符'
    }

    if (!confirmPassword) {
      errors.confirmPassword = '请确认密码'
    } else if (password !== confirmPassword) {
      errors.confirmPassword = '两次输入的密码不一致'
    }

    setFormErrors(errors)
    return Object.keys(errors).length === 0
  }

  /**
   * 处理注册提交
   */
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()

    if (!validateForm()) {
      return
    }

    try {
      await register(username.trim(), password, email.trim())
      // 注册成功后自动登录，跳转到首页
      // 登录状态由 authStore 自动处理，isAuthenticated 变化会触发跳转
    } catch {
      // 错误已在 store 中处理
    }
  }

  return (
    <div
      className="min-h-screen flex items-center justify-center bg-background px-4 py-12"
      data-testid="register-page"
    >
      <div className="w-full max-w-md space-y-6">
        {/* 标题 */}
        <div className="text-center space-y-2">
          <h1 className="text-3xl font-bold text-foreground">注册</h1>
          <p className="text-muted-foreground">创建您的账号，开始使用</p>
        </div>

        {/* 注册表单 */}
        <form
          onSubmit={handleSubmit}
          className="space-y-5"
          data-testid="register-form"
        >
          {/* 全局错误提示 */}
          {error && (
            <div
              className="p-3 rounded-lg bg-destructive/10 text-destructive text-sm"
              data-testid="register-error"
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
              data-testid="register-username-input"
              className="h-10 min-h-[40px]"
            />
            {formErrors.username && (
              <p
                id="username-error"
                className="text-sm text-destructive min-h-[20px]"
                data-testid="register-username-error"
              >
                {formErrors.username}
              </p>
            )}
          </div>

          {/* 邮箱输入 */}
          <div className="space-y-2">
            <label
              htmlFor="email"
              className="text-sm font-medium text-foreground block"
            >
              邮箱
            </label>
            <Input
              id="email"
              type="email"
              value={email}
              onChange={e => setEmail(e.target.value)}
              placeholder="请输入邮箱"
              disabled={isLoading}
              aria-invalid={!!formErrors.email}
              aria-describedby={formErrors.email ? 'email-error' : undefined}
              data-testid="email-input"
              className="h-10 min-h-[40px]"
            />
            {formErrors.email && (
              <p
                id="email-error"
                className="text-sm text-destructive min-h-[20px]"
                data-testid="email-error"
              >
                {formErrors.email}
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
              data-testid="register-password-input"
              className="h-10 min-h-[40px]" />
            {formErrors.password && (
              <p
                id="password-error"
                className="text-sm text-destructive min-h-[20px]"
                data-testid="register-password-error"
              >
                {formErrors.password}
              </p>
            )}
          </div>

          {/* 确认密码输入 */}
          <div className="space-y-2">
            <label
              htmlFor="confirmPassword"
              className="text-sm font-medium text-foreground block"
            >
              确认密码
            </label>
            <Input
              id="confirmPassword"
              type="password"
              value={confirmPassword}
              onChange={e => setConfirmPassword(e.target.value)}
              placeholder="请再次输入密码"
              disabled={isLoading}
              aria-invalid={!!formErrors.confirmPassword}
              aria-describedby={
                formErrors.confirmPassword ? 'confirmPassword-error' : undefined
              }
              data-testid="confirm-password-input"
              className="h-10 min-h-[40px]"
            />
            {formErrors.confirmPassword && (
              <p
                id="confirmPassword-error"
                className="text-sm text-destructive min-h-[20px]"
                data-testid="confirm-password-error"
              >
                {formErrors.confirmPassword}
              </p>
            )}
          </div>

          {/* 注册按钮 */}
          <Button
            type="submit"
            className="w-full h-10 mt-2"
            disabled={isLoading}
            data-testid="register-submit-button"
          >
            {isLoading ? '注册中...' : '注册'}
          </Button>
        </form>

        {/* 登录链接 */}
        <p className="text-center text-sm text-muted-foreground pt-2">
          已有账号？{' '}
          <Link
            to={ROUTES.LOGIN}
            className="font-medium text-primary hover:underline"
            data-testid="login-link"
          >
            登录
          </Link>
        </p>
      </div>
    </div>
  )
}

export default RegisterPage
