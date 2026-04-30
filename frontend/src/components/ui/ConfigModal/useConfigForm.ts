/**
 * 配置表单 Hook
 *
 * 提供表单状态管理、验证和 localStorage 暂存功能
 */

import { useCallback, useEffect, useState } from 'react'
import type { ConfigField, FormErrors, FormState } from './types'

/**
 * 获取 localStorage 暂存数据的 key
 */
const getDraftKey = (configType: string) => `config_draft_${configType}`

/**
 * 从 localStorage 加载暂存数据
 */
const loadDraft = <T extends object>(configType: string): T | null => {
  try {
    const key = getDraftKey(configType)
    const draft = localStorage.getItem(key)
    if (draft) {
      return JSON.parse(draft) as T
    }
  } catch (error) {
    console.warn('加载暂存数据失败:', error)
  }
  return null
}

/**
 * 保存数据到 localStorage
 */
const saveDraft = <T extends object>(configType: string, data: T) => {
  try {
    const key = getDraftKey(configType)
    localStorage.setItem(key, JSON.stringify(data))
  } catch (error) {
    console.warn('保存暂存数据失败:', error)
  }
}

/**
 * 清除 localStorage 暂存数据
 */
export const clearDraft = (configType: string) => {
  try {
    const key = getDraftKey(configType)
    localStorage.removeItem(key)
  } catch (error) {
    console.warn('清除暂存数据失败:', error)
  }
}

/**
 * 验证单个字段
 */
const validateField = <T extends object>(
  field: ConfigField<T>,
  value: unknown,
  formData: T,
): string | null => {
  // 必填验证
  if (field.required) {
    if (value === undefined || value === null || value === '') {
      return `${field.label}不能为空`
    }
  }

  // 类型验证
  if (value !== undefined && value !== null && value !== '') {
    switch (field.type) {
      case 'number': {
        const num = Number(value)
        if (isNaN(num)) {
          return `${field.label}必须是有效数字`
        }
        if (field.min !== undefined && num < field.min) {
          return `${field.label}不能小于 ${field.min}`
        }
        if (field.max !== undefined && num > field.max) {
          return `${field.label}不能大于 ${field.max}`
        }
        break
      }
      case 'json': {
        try {
          if (typeof value === 'string' && value.trim()) {
            JSON.parse(value)
          }
        } catch {
          return `${field.label}必须是有效的 JSON 格式`
        }
        break
      }
    }
  }

  // 自定义验证
  if (field.validate) {
    return field.validate(value, formData)
  }

  return null
}

/**
 * 验证所有字段
 */
const validateForm = <T extends object>(fields: ConfigField<T>[], formData: T): FormErrors<T> => {
  const errors: FormErrors<T> = {}

  for (const field of fields) {
    const value = formData[field.key]
    const error = validateField(field, value, formData)
    if (error) {
      errors[field.key] = error
    }
  }

  return errors
}

/**
 * 配置表单 Hook
 *
 * 提供表单状态管理、验证和暂存功能
 */
export function useConfigForm<T extends object>(
  fields: ConfigField<T>[],
  initialData: T,
  configType?: string,
) {
  const [state, setState] = useState<FormState<T>>({
    data: initialData,
    errors: {},
    isDirty: false,
    isSubmitting: false,
  })

  // 初始化时加载暂存数据
  useEffect(() => {
    if (configType) {
      const draft = loadDraft<T>(configType)
      if (draft) {
        setState((prev) => ({
          ...prev,
          data: draft,
          isDirty: true,
        }))
      }
    }
  }, [configType])

  // 当初始数据变化时更新（但保留暂存数据优先）
  useEffect(() => {
    if (configType) {
      const draft = loadDraft<T>(configType)
      if (!draft) {
        setState((prev) => ({
          ...prev,
          data: initialData,
        }))
      }
    } else {
      setState((prev) => ({
        ...prev,
        data: initialData,
      }))
    }
  }, [initialData, configType])

  /**
   * 更新单个字段值
   */
  const updateField = useCallback(
    (key: keyof T, value: unknown) => {
      setState((prev) => {
        const newData = { ...prev.data, [key]: value } as T

        // 保存暂存
        if (configType) {
          saveDraft(configType, newData)
        }

        // 实时验证该字段
        const field = fields.find((f) => f.key === key)
        const errors = { ...prev.errors }
        if (field) {
          const error = validateField(field, value, newData)
          if (error) {
            errors[key] = error
          } else {
            delete errors[key]
          }
        }

        return {
          ...prev,
          data: newData,
          errors,
          isDirty: true,
        }
      })
    },
    [fields, configType],
  )

  /**
   * 批量更新字段值
   */
  const updateFields = useCallback(
    (updates: Partial<T>) => {
      setState((prev) => {
        const newData = { ...prev.data, ...updates } as T

        // 保存暂存
        if (configType) {
          saveDraft(configType, newData)
        }

        return {
          ...prev,
          data: newData,
          isDirty: true,
        }
      })
    },
    [configType],
  )

  /**
   * 重置表单
   */
  const reset = useCallback(() => {
    setState({
      data: initialData,
      errors: {},
      isDirty: false,
      isSubmitting: false,
    })

    // 清除暂存
    if (configType) {
      clearDraft(configType)
    }
  }, [initialData, configType])

  /**
   * 验证整个表单
   */
  const validate = useCallback((): boolean => {
    const errors = validateForm(fields, state.data)
    setState((prev) => ({ ...prev, errors }))
    return Object.keys(errors).length === 0
  }, [fields, state.data])

  /**
   * 设置提交状态
   */
  const setSubmitting = useCallback((isSubmitting: boolean) => {
    setState((prev) => ({ ...prev, isSubmitting }))
  }, [])

  /**
   * 清除暂存数据
   */
  const clearStorage = useCallback(() => {
    if (configType) {
      clearDraft(configType)
    }
  }, [configType])

  return {
    data: state.data,
    errors: state.errors,
    isDirty: state.isDirty,
    isSubmitting: state.isSubmitting,
    updateField,
    updateFields,
    reset,
    validate,
    setSubmitting,
    clearStorage,
  }
}
