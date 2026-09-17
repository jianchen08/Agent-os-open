/**
 * 注册页面
 *
 * 用户名/邮箱/密码/确认密码表单 + 校验 + 注册状态处理。壳/字段/校验
 * 状态机为 authForm 共享件，此处只声明字段与注册语义。
 */

import { AuthField, AuthPageShell } from './authForm'
import { useAuthForm, useAuthPageLifecycle, type AuthFieldDefinition } from './authFormModel'
import { ROUTES } from '../../constants/routes'
import { useAuthStore } from '../../stores/authStore'
import type { FormEvent } from 'react'

/** 注册字段与校验规则（用户名白名单与后端 is_valid_username 对齐） */
const FIELDS: AuthFieldDefinition[] = [
  {
    id: 'username',
    label: '用户名',
    type: 'text',
    placeholder: '请输入用户名',
    validate: (value) => {
      if (!value.trim()) return '用户名不能为空'
      if (value.length < 3) return '用户名至少3个字符'
      // 与后端注册白名单对齐（agentos_http::auth is_valid_username）：
      // username 进入 `:` 分隔的 token 载荷，越界字符会导致账户会话异常
      if (value.length > 64 || !/^[a-zA-Z0-9._@-]+$/.test(value)) {
        return '用户名仅支持字母、数字、下划线、点、连字符和 @，不超过64个字符'
      }
      return undefined
    },
    inputTestId: 'register-username-input',
    errorTestId: 'register-username-error',
  },
  {
    id: 'email',
    label: '邮箱',
    type: 'email',
    placeholder: '请输入邮箱',
    validate: (value) => {
      if (!value.trim()) return '邮箱不能为空'
      if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value)) return '请输入有效的邮箱地址'
      return undefined
    },
    inputTestId: 'email-input',
    errorTestId: 'email-error',
  },
  {
    id: 'password',
    label: '密码',
    type: 'password',
    placeholder: '请输入密码',
    validate: (value) => {
      if (!value) return '密码不能为空'
      if (value.length < 6) return '密码至少6个字符'
      return undefined
    },
    inputTestId: 'register-password-input',
    errorTestId: 'register-password-error',
  },
  {
    id: 'confirmPassword',
    label: '确认密码',
    type: 'password',
    placeholder: '请再次输入密码',
    validate: (value, values) => {
      if (!value) return '请确认密码'
      if (values.password !== value) return '两次输入的密码不一致'
      return undefined
    },
    inputTestId: 'confirm-password-input',
    errorTestId: 'confirm-password-error',
  },
]

/**
 * 注册页面组件
 */
export function RegisterPage() {
  const { register, isLoading, error, isAuthenticated, clearError } = useAuthStore()
  const { values, setValue, errors, handleBlur, validateAll } = useAuthForm(FIELDS)
  useAuthPageLifecycle(isAuthenticated, clearError)

  /**
   * 处理注册提交
   */
  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()

    if (!validateAll()) {
      return
    }

    try {
      await register(values.username.trim(), values.password, values.email.trim())
      // 注册成功后自动登录，跳转到首页
      // 登录状态由 authStore 自动处理，isAuthenticated 变化会触发跳转
    } catch {
      // 错误已在 store 中处理
    }
  }

  return (
    <AuthPageShell
      pageTestId="register-page"
      title="注册"
      subtitle="创建您的账号，开始使用"
      formLabel="注册表单"
      formTestId="register-form"
      error={error}
      errorTestId="register-error"
      isLoading={isLoading}
      submitText="注册"
      submitLoadingText="注册中..."
      submitTestId="register-submit-button"
      switchPrompt="已有账号？"
      switchLinkText="登录"
      switchLinkTo={ROUTES.LOGIN}
      switchLinkTestId="login-link"
      onSubmit={handleSubmit}
    >
      {FIELDS.map((field) => (
        <AuthField
          key={field.id}
          definition={field}
          value={values[field.id] ?? ''}
          error={errors[field.id]}
          disabled={isLoading}
          onChange={(value) => setValue(field.id, value)}
          onBlur={() => handleBlur(field)}
        />
      ))}
    </AuthPageShell>
  )
}

export default RegisterPage
