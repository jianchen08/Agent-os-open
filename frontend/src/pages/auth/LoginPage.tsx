/**
 * 登录页面
 *
 * 用户名/密码表单 + 校验 + 登录状态处理。壳/字段/校验状态机为
 * authForm 共享件，此处只声明字段与登录语义。
 */

import { useNavigate } from 'react-router-dom'
import { AuthField, AuthPageShell } from './authForm'
import { useAuthForm, useAuthPageLifecycle, type AuthFieldDefinition } from './authFormModel'
import { ROUTES } from '../../constants/routes'
import { useAuthStore } from '../../stores/authStore'
import type { FormEvent } from 'react'

/** 登录字段与校验规则 */
const FIELDS: AuthFieldDefinition[] = [
  {
    id: 'username',
    label: '用户名',
    type: 'text',
    placeholder: '请输入用户名',
    validate: (value) => (!value.trim() ? '用户名不能为空' : undefined),
    inputTestId: 'login-username-input',
    errorTestId: 'username-error',
  },
  {
    id: 'password',
    label: '密码',
    type: 'password',
    placeholder: '请输入密码',
    validate: (value) => (!value ? '密码不能为空' : undefined),
    inputTestId: 'login-password-input',
    errorTestId: 'password-error',
  },
]

/**
 * 登录页面组件
 */
export function LoginPage() {
  const navigate = useNavigate()
  const { login, isLoading, error, isAuthenticated, clearError } = useAuthStore()
  const { values, setValue, errors, handleBlur, validateAll } = useAuthForm(FIELDS)
  useAuthPageLifecycle(isAuthenticated, clearError)

  /**
   * 处理登录提交
   */
  const handleSubmit = async (e: FormEvent) => {
    e.preventDefault()

    if (!validateAll()) {
      return
    }

    try {
      await login(values.username.trim(), values.password)
      navigate(ROUTES.HOME)
    } catch {
      // 错误已在 store 中处理
    }
  }

  return (
    <AuthPageShell
      pageTestId="login-page"
      title="登录"
      subtitle="欢迎回来，请登录您的账号"
      formLabel="登录表单"
      formTestId="login-form"
      error={error}
      errorTestId="login-error"
      isLoading={isLoading}
      submitText="登录"
      submitLoadingText="登录中..."
      submitTestId="login-submit-button"
      switchPrompt="没有账号？"
      switchLinkText="注册"
      switchLinkTo={ROUTES.REGISTER}
      switchLinkTestId="register-link"
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

export default LoginPage
