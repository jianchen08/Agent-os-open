/**
 * 认证表单状态机（登录/注册页同构逻辑收口，2026-09-16 规则驱动审查 F8）。
 *
 * 字段值集 / 失焦单字段校验 / 整表校验聚合 + 两页同款生命周期
 * （已认证自动回首页、卸载清 store 错误）。渲染件见 authForm.tsx；
 * 字段定义与校验规则由各页声明（页面自有语义，跨字段校验经 values 透传）。
 */

import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { ROUTES } from '../../constants/routes'

/** 字段定义：id/文案/校验/testid 均由页面声明 */
export interface AuthFieldDefinition {
  id: string
  label: string
  type: 'text' | 'password' | 'email'
  placeholder: string
  /** 返回错误文案；undefined = 通过。values 供跨字段校验（确认密码等） */
  validate: (value: string, values: Record<string, string>) => string | undefined
  inputTestId: string
  errorTestId: string
}

/** 认证表单状态机：值集 / 失焦单字段校验 / 整表校验（登录注册同契约） */
export function useAuthForm(definitions: AuthFieldDefinition[]) {
  const [values, setValues] = useState<Record<string, string>>(() =>
    Object.fromEntries(definitions.map((d) => [d.id, ''])),
  )
  const [errors, setErrors] = useState<Record<string, string>>({})

  const setValue = (id: string, value: string) =>
    setValues((prev) => ({ ...prev, [id]: value }))

  const validateField = (d: AuthFieldDefinition) =>
    d.validate(values[d.id] ?? '', values)

  const handleBlur = (d: AuthFieldDefinition) => {
    const error = validateField(d)
    setErrors((prev) => {
      const next = { ...prev }
      if (error) {
        next[d.id] = error
      } else {
        delete next[d.id]
      }
      return next
    })
  }

  const validateAll = (): boolean => {
    const next: Record<string, string> = {}
    for (const d of definitions) {
      const error = validateField(d)
      if (error) {
        next[d.id] = error
      }
    }
    setErrors(next)
    return Object.keys(next).length === 0
  }

  return { values, setValue, errors, handleBlur, validateAll }
}

/** 已认证自动回首页 + 卸载清 store 错误（两页同款生命周期） */
export function useAuthPageLifecycle(
  isAuthenticated: boolean,
  clearError: () => void,
): void {
  const navigate = useNavigate()
  useEffect(() => {
    if (isAuthenticated) {
      navigate(ROUTES.HOME)
    }
  }, [isAuthenticated, navigate])

  useEffect(() => {
    return () => {
      clearError()
    }
  }, [clearError])
}
