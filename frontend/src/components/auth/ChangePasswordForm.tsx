/**
 * 修改口令表单（D1-3）
 *
 * 验旧口令 → 服务端写新哈希并吊销其他会话 → 当前会话以响应中的新 token 对
 * 无感续期。供两处复用：首登强制改密闸（ChangePasswordGate）与侧边栏用户
 * 菜单的账户设置入口（Dialog 内）。
 */

import { useState } from 'react'
import { useAuthStore } from '../../stores/authStore'
import { Button } from '../ui/button'
import { Input } from '../ui/input'

/** 表单错误类型 */
interface FormErrors {
  oldPassword?: string
  newPassword?: string
  confirmPassword?: string
}

export function ChangePasswordForm({ onSuccess }: { onSuccess?: () => void }) {
  const { changePassword, isLoading } = useAuthStore()
  const [oldPassword, setOldPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [formErrors, setFormErrors] = useState<FormErrors>({})
  const [submitError, setSubmitError] = useState<string | null>(null)
  const [succeeded, setSucceeded] = useState(false)

  const validateField = (field: keyof FormErrors): string | undefined => {
    switch (field) {
      case 'oldPassword':
        return !oldPassword ? '旧口令不能为空' : undefined
      case 'newPassword':
        return !newPassword
          ? '新口令不能为空'
          : newPassword.length < 8
            ? '新口令长度至少为8个字符'
            : undefined
      case 'confirmPassword':
        return confirmPassword !== newPassword ? '两次输入的新口令不一致' : undefined
      default:
        return undefined
    }
  }

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

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    const errors: FormErrors = {}
    for (const field of ['oldPassword', 'newPassword', 'confirmPassword'] as const) {
      const error = validateField(field)
      if (error) errors[field] = error
    }
    setFormErrors(errors)
    if (Object.keys(errors).length > 0) return

    try {
      await changePassword(oldPassword, newPassword)
      setSucceeded(true)
      onSuccess?.()
    } catch (error: unknown) {
      setSubmitError(error instanceof Error ? error.message : '修改口令失败')
    }
  }

  if (succeeded) {
    return (
      <div className="text-status-success space-y-3 text-sm" data-testid="change-password-success">
        口令已修改。其他设备的登录会话已被吊销，请使用新口令重新登录其他设备。
      </div>
    )
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4" role="form" aria-label="修改口令表单" data-testid="change-password-form">
      {submitError && (
        <div className="bg-destructive/10 text-destructive rounded-lg p-3 text-sm" data-testid="change-password-error">
          {submitError}
        </div>
      )}

      <div className="space-y-2">
        <label htmlFor="old-password" className="text-foreground block text-sm font-medium">
          旧口令 <span className="text-destructive">*</span>
        </label>
        <Input
          id="old-password"
          type="password"
          value={oldPassword}
          onChange={(e) => setOldPassword(e.target.value)}
          onBlur={() => handleBlur('oldPassword')}
          placeholder="请输入当前口令"
          disabled={isLoading}
          aria-invalid={!!formErrors.oldPassword}
          data-testid="change-password-old-input"
          className={`h-10 min-h-[40px] ${formErrors.oldPassword ? 'border-destructive' : ''}`}
        />
        {formErrors.oldPassword && (
          <p className="text-destructive min-h-[20px] text-sm" data-testid="old-password-error">
            {formErrors.oldPassword}
          </p>
        )}
      </div>

      <div className="space-y-2">
        <label htmlFor="new-password" className="text-foreground block text-sm font-medium">
          新口令 <span className="text-destructive">*</span>
        </label>
        <Input
          id="new-password"
          type="password"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          onBlur={() => handleBlur('newPassword')}
          placeholder="至少8个字符"
          disabled={isLoading}
          aria-invalid={!!formErrors.newPassword}
          data-testid="change-password-new-input"
          className={`h-10 min-h-[40px] ${formErrors.newPassword ? 'border-destructive' : ''}`}
        />
        {formErrors.newPassword && (
          <p className="text-destructive min-h-[20px] text-sm" data-testid="new-password-error">
            {formErrors.newPassword}
          </p>
        )}
      </div>

      <div className="space-y-2">
        <label htmlFor="confirm-password" className="text-foreground block text-sm font-medium">
          确认新口令 <span className="text-destructive">*</span>
        </label>
        <Input
          id="confirm-password"
          type="password"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          onBlur={() => handleBlur('confirmPassword')}
          placeholder="再次输入新口令"
          disabled={isLoading}
          aria-invalid={!!formErrors.confirmPassword}
          data-testid="change-password-confirm-input"
          className={`h-10 min-h-[40px] ${formErrors.confirmPassword ? 'border-destructive' : ''}`}
        />
        {formErrors.confirmPassword && (
          <p className="text-destructive min-h-[20px] text-sm" data-testid="confirm-password-error">
            {formErrors.confirmPassword}
          </p>
        )}
      </div>

      <Button type="submit" className="h-10 w-full" disabled={isLoading} data-testid="change-password-submit-button">
        {isLoading ? '提交中...' : '修改口令'}
      </Button>
    </form>
  )
}
