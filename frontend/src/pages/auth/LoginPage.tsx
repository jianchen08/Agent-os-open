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
import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'
import { ROUTES } from '../../constants/routes'
import { useAuthStore } from '../../stores/authStore'

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
  const { login, isLoading, error, isAuthenticated, clearError } = useAuthStore()

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
   * 验证单个字段
   */
  const validateField = (field: keyof FormErrors): string | undefined => {
    switch (field) {
      case 'username':
        return !username.trim() ? '用户名不能为空' : undefined
      case 'password':
        return !password ? '密码不能为空' : undefined
      default:
        return undefined
    }
  }

  /**
   * 处理字段失焦验证
   */
  const handleBlur = (field: keyof FormErrors) => {
    const error = validateField(field)
    setFormErrors((prev) => {
      const next = { ...prev }
      if (error) {
        next[field] = error
      } else {
        delete next[field]
      }
      return next
    })
  }

  /**
   * 验证表单
   */
  const validateForm = (): boolean => {
    const errors: FormErrors = {}
    const usernameError = validateField('username')
    if (usernameError) errors.username = usernameError
    const passwordError = validateField('password')
    if (passwordError) errors.password = passwordError

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
      className="bg-background text-foreground flex min-h-screen items-center justify-center px-4 py-12"
      data-testid="login-page"
    >
      <div className="w-full max-w-md space-y-6">
        {/* 标题 */}
        <div className="space-y-2 text-center">
          <h1 className="text-foreground text-3xl font-bold">登录</h1>
          <p className="text-muted-foreground">欢迎回来，请登录您的账号</p>
        </div>

        {/* 登录表单 */}
        <form onSubmit={handleSubmit} className="space-y-5" data-testid="login-form" role="form" aria-label="登录表单">
          {/* 全局错误提示 */}
          {error && (
            <div
              className="bg-destructive/10 text-destructive rounded-lg p-3 text-sm"
              data-testid="login-error"
            >
              {error}
            </div>
          )}

          {/* 用户名输入 */}
          <div className="space-y-2">
            <label htmlFor="username" className="text-foreground block text-sm font-medium">
              用户名 <span className="text-destructive">*</span>
            </label>
            <Input
              id="username"
              type="text"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              onBlur={() => handleBlur('username')}
              placeholder="请输入用户名"
              disabled={isLoading}
              aria-invalid={!!formErrors.username}
              aria-describedby={formErrors.username ? 'username-error' : undefined}
              data-testid="login-username-input"
              className={`h-10 min-h-[40px] ${formErrors.username ? 'border-destructive' : ''}`}
            />
            {formErrors.username && (
              <p
                id="username-error"
                className="text-destructive min-h-[20px] text-sm"
                data-testid="username-error"
              >
                {formErrors.username}
              </p>
            )}
          </div>

          {/* 密码输入 */}
          <div className="space-y-2">
            <label htmlFor="password" className="text-foreground block text-sm font-medium">
              密码 <span className="text-destructive">*</span>
            </label>
            <Input
              id="password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              onBlur={() => handleBlur('password')}
              placeholder="请输入密码"
              disabled={isLoading}
              aria-invalid={!!formErrors.password}
              aria-describedby={formErrors.password ? 'password-error' : undefined}
              data-testid="login-password-input"
              className={`h-10 min-h-[40px] ${formErrors.password ? 'border-destructive' : ''}`}
            />
            {formErrors.password && (
              <p
                id="password-error"
                className="text-destructive min-h-[20px] text-sm"
                data-testid="password-error"
              >
                {formErrors.password}
              </p>
            )}
          </div>

          {/* 登录按钮 */}
          <Button
            type="submit"
            className="mt-2 h-10 w-full"
            disabled={isLoading}
            data-testid="login-submit-button"
          >
            {isLoading ? '登录中...' : '登录'}
          </Button>
        </form>

        {/* 注册链接 */}
        <p className="text-muted-foreground pt-2 text-center text-sm">
          没有账号？{' '}
          <Link
            to={ROUTES.REGISTER}
            className="text-primary font-medium hover:underline"
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
