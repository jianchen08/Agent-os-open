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
import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'
import { ROUTES } from '../../constants/routes'
import { useAuthStore } from '../../stores/authStore'

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
  const { register, isLoading, error, isAuthenticated, clearError } = useAuthStore()

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
   * 验证单个字段
   */
  const validateField = (field: keyof FormErrors): string | undefined => {
    switch (field) {
      case 'username':
        if (!username.trim()) return '用户名不能为空'
        if (username.length < 3) return '用户名至少3个字符'
        return undefined
      case 'email':
        if (!email.trim()) return '邮箱不能为空'
        if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return '请输入有效的邮箱地址'
        return undefined
      case 'password':
        if (!password) return '密码不能为空'
        if (password.length < 6) return '密码至少6个字符'
        return undefined
      case 'confirmPassword':
        if (!confirmPassword) return '请确认密码'
        if (password !== confirmPassword) return '两次输入的密码不一致'
        return undefined
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
    const emailError = validateField('email')
    if (emailError) errors.email = emailError
    const passwordError = validateField('password')
    if (passwordError) errors.password = passwordError
    const confirmError = validateField('confirmPassword')
    if (confirmError) errors.confirmPassword = confirmError

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
      className="bg-background text-foreground flex min-h-screen items-center justify-center px-4 py-12"
      data-testid="register-page"
    >
      <div className="w-full max-w-md space-y-6">
        {/* 标题 */}
        <div className="space-y-2 text-center">
          <h1 className="text-foreground text-3xl font-bold">注册</h1>
          <p className="text-muted-foreground">创建您的账号，开始使用</p>
        </div>

        {/* 注册表单 */}
        <form onSubmit={handleSubmit} className="space-y-5" data-testid="register-form" role="form" aria-label="注册表单">
          {/* 全局错误提示 */}
          {error && (
            <div
              className="bg-destructive/10 text-destructive rounded-lg p-3 text-sm"
              data-testid="register-error"
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
              data-testid="register-username-input"
              className={`h-10 min-h-[40px] ${formErrors.username ? 'border-destructive' : ''}`}
            />
            {formErrors.username && (
              <p
                id="username-error"
                className="text-destructive min-h-[20px] text-sm"
                data-testid="register-username-error"
              >
                {formErrors.username}
              </p>
            )}
          </div>

          {/* 邮箱输入 */}
          <div className="space-y-2">
            <label htmlFor="email" className="text-foreground block text-sm font-medium">
              邮箱 <span className="text-destructive">*</span>
            </label>
            <Input
              id="email"
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              onBlur={() => handleBlur('email')}
              placeholder="请输入邮箱"
              disabled={isLoading}
              aria-invalid={!!formErrors.email}
              aria-describedby={formErrors.email ? 'email-error' : undefined}
              data-testid="email-input"
              className={`h-10 min-h-[40px] ${formErrors.email ? 'border-destructive' : ''}`}
            />
            {formErrors.email && (
              <p
                id="email-error"
                className="text-destructive min-h-[20px] text-sm"
                data-testid="email-error"
              >
                {formErrors.email}
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
              data-testid="register-password-input"
              className={`h-10 min-h-[40px] ${formErrors.password ? 'border-destructive' : ''}`}
            />
            {formErrors.password && (
              <p
                id="password-error"
                className="text-destructive min-h-[20px] text-sm"
                data-testid="register-password-error"
              >
                {formErrors.password}
              </p>
            )}
          </div>

          {/* 确认密码输入 */}
          <div className="space-y-2">
            <label htmlFor="confirmPassword" className="text-foreground block text-sm font-medium">
              确认密码 <span className="text-destructive">*</span>
            </label>
            <Input
              id="confirmPassword"
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              onBlur={() => handleBlur('confirmPassword')}
              placeholder="请再次输入密码"
              disabled={isLoading}
              aria-invalid={!!formErrors.confirmPassword}
              aria-describedby={formErrors.confirmPassword ? 'confirmPassword-error' : undefined}
              data-testid="confirm-password-input"
              className={`h-10 min-h-[40px] ${formErrors.confirmPassword ? 'border-destructive' : ''}`}
            />
            {formErrors.confirmPassword && (
              <p
                id="confirmPassword-error"
                className="text-destructive min-h-[20px] text-sm"
                data-testid="confirm-password-error"
              >
                {formErrors.confirmPassword}
              </p>
            )}
          </div>

          {/* 注册按钮 */}
          <Button
            type="submit"
            className="mt-2 h-10 w-full"
            disabled={isLoading}
            data-testid="register-submit-button"
          >
            {isLoading ? '注册中...' : '注册'}
          </Button>
        </form>

        {/* 登录链接 */}
        <p className="text-muted-foreground pt-2 text-center text-sm">
          已有账号？{' '}
          <Link
            to={ROUTES.LOGIN}
            className="text-primary font-medium hover:underline"
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
