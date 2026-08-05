/**
 * LoginModal 弹窗式登录框
 *
 * 在当前页弹出登录表单(不跳转 /login),复用 useAuthStore.login。
 * 用于侧边栏用户区:未登录点击 → 弹出本框;已登录可在用户菜单里"切换账号"再次弹出。
 */

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Modal } from '@/components/ui/Modal'
import { useEffect, useRef, useState } from 'react'
import { useAuthStore } from '@/stores/authStore'

interface FormErrors {
  username?: string
  password?: string
}

export interface LoginModalProps {
  /** 是否显示 */
  open: boolean
  /** 关闭回调 */
  onClose: () => void
}

/** 弹窗式登录框 */
export function LoginModal({ open, onClose }: LoginModalProps) {
  const { login, isLoading, error, isAuthenticated, clearError } = useAuthStore()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [formErrors, setFormErrors] = useState<FormErrors>({})

  // 登录成功 → 自动关闭弹窗并清空表单。
  // 关键:只在"登录态从 false → true"(真正的登录动作)时关闭,
  // 已登录态打开弹窗(切换账号)时不触发关闭,否则一打开就被关掉。
  const wasAuthenticatedRef = useRef(false)
  useEffect(() => {
    if (open && isAuthenticated && !wasAuthenticatedRef.current) {
      handleClose()
    }
    wasAuthenticatedRef.current = isAuthenticated
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated, open])

  // 打开时清除上次错误
  useEffect(() => {
    if (open) {
      clearError()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  const handleClose = () => {
    setUsername('')
    setPassword('')
    setFormErrors({})
    clearError()
    onClose()
  }

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

  const validateForm = (): boolean => {
    const errors: FormErrors = {}
    const usernameError = validateField('username')
    if (usernameError) errors.username = usernameError
    const passwordError = validateField('password')
    if (passwordError) errors.password = passwordError
    setFormErrors(errors)
    return Object.keys(errors).length === 0
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!validateForm()) return
    try {
      await login(username.trim(), password)
      // 登录成功由 useEffect 监听 isAuthenticated 关闭弹窗
    } catch {
      // 错误已在 store 中处理,弹窗内显示
    }
  }

  return (
    <Modal open={open} onClose={handleClose} title="登录" maxWidth="sm" showClose>
      <form onSubmit={handleSubmit} className="space-y-4" data-testid="login-modal-form">
        {/* 全局错误提示 */}
        {error && (
          <div
            className="bg-destructive/10 text-destructive rounded-lg p-2.5 text-sm"
            data-testid="login-modal-error"
          >
            {error}
          </div>
        )}

        {/* 用户名 */}
        <div className="space-y-1.5">
          <label htmlFor="login-modal-username" className="text-foreground block text-sm font-medium">
            用户名 <span className="text-destructive">*</span>
          </label>
          <Input
            id="login-modal-username"
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="请输入用户名"
            disabled={isLoading}
            aria-invalid={!!formErrors.username}
            data-testid="login-modal-username"
            className="h-9"
            autoFocus
          />
          {formErrors.username && (
            <p className="text-destructive text-xs">{formErrors.username}</p>
          )}
        </div>

        {/* 密码 */}
        <div className="space-y-1.5">
          <label htmlFor="login-modal-password" className="text-foreground block text-sm font-medium">
            密码 <span className="text-destructive">*</span>
          </label>
          <Input
            id="login-modal-password"
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="请输入密码"
            disabled={isLoading}
            aria-invalid={!!formErrors.password}
            data-testid="login-modal-password"
            className="h-9"
          />
          {formErrors.password && (
            <p className="text-destructive text-xs">{formErrors.password}</p>
          )}
        </div>

        <Button type="submit" className="h-9 w-full" disabled={isLoading} data-testid="login-modal-submit">
          {isLoading ? '登录中...' : '登录'}
        </Button>
      </form>
    </Modal>
  )
}

export default LoginModal
