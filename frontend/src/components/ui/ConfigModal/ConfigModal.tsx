/**
 * ConfigModal 组件
 *
 * 统一的配置模态框组件，支持多种字段类型、验证和暂存功能
 */

import { X, Save, RotateCcw, AlertCircle } from 'lucide-react'
import { useCallback, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { cn } from '@/lib/utils'
import { useConfigForm } from './useConfigForm'
import type { ConfigModalProps, ConfigField } from './types'

/**
 * 最大宽度映射（使用固定像素值确保不会撑满页面）
 */
const maxWidthStyles: Record<string, React.CSSProperties> = {
  sm: { maxWidth: '384px' }, // 24rem
  md: { maxWidth: '448px' }, // 28rem
  lg: { maxWidth: '512px' }, // 32rem
  xl: { maxWidth: '576px' }, // 36rem
  '2xl': { maxWidth: '672px' }, // 42rem
  '3xl': { maxWidth: '768px' }, // 48rem
  '4xl': { maxWidth: '896px' }, // 56rem
  full: { maxWidth: '95vw' },
}

/**
 * 渲染单个字段
 */
function renderField<T extends object>(
  field: ConfigField<T>,
  value: unknown,
  error: string | undefined,
  onChange: (key: keyof T, value: unknown) => void,
) {
  const {
    key,
    label,
    type,
    placeholder,
    required,
    options,
    min,
    max,
    step,
    description,
    disabled,
    rows = 4,
  } = field

  // 通用字段包装器 - 添加文本换行支持
  const FieldWrapper = ({ children }: { children: React.ReactNode }) => (
    <div className="min-w-0 space-y-1.5">
      <label className="text-foreground flex flex-wrap items-center gap-1 text-sm font-medium">
        <span className="break-words">{label}</span>
        {required && <span className="text-destructive flex-shrink-0">*</span>}
      </label>
      {children}
      {description && (
        <p className="text-muted-foreground overflow-wrap-anywhere text-xs break-words whitespace-normal">
          {description}
        </p>
      )}
      {error && (
        <p className="text-destructive flex items-start gap-1 text-xs break-words">
          <AlertCircle className="mt-0.5 h-3 w-3 flex-shrink-0" />
          <span className="break-words">{error}</span>
        </p>
      )}
    </div>
  )

  // 根据类型渲染不同控件
  switch (type) {
    case 'text':
    case 'password':
      return (
        <FieldWrapper key={String(key)}>
          <Input
            type={type}
            value={(value as string) || ''}
            onChange={(e) => onChange(key, e.target.value)}
            placeholder={placeholder}
            disabled={disabled}
            className={cn(error && 'border-destructive')}
          />
        </FieldWrapper>
      )

    case 'number':
      return (
        <FieldWrapper key={String(key)}>
          <Input
            type="number"
            value={(value as number) ?? ''}
            onChange={(e) => onChange(key, e.target.value ? Number(e.target.value) : undefined)}
            placeholder={placeholder}
            disabled={disabled}
            min={min}
            max={max}
            step={step}
            className={cn(error && 'border-destructive')}
          />
        </FieldWrapper>
      )

    case 'textarea':
      return (
        <FieldWrapper key={String(key)}>
          <textarea
            value={(value as string) || ''}
            onChange={(e) => onChange(key, e.target.value)}
            placeholder={placeholder}
            disabled={disabled}
            rows={rows}
            className={cn(
              'border-input bg-background w-full resize-none rounded-xl border-2 px-3 py-2 text-sm',
              'focus-visible:border-primary focus-visible:ring-primary/20 focus-visible:ring-2 focus-visible:outline-none',
              'disabled:cursor-not-allowed disabled:opacity-50',
              'break-words whitespace-pre-wrap',
              error && 'border-destructive',
            )}
          />
        </FieldWrapper>
      )

    case 'json':
      return (
        <FieldWrapper key={String(key)}>
          <textarea
            value={(value as string) || ''}
            onChange={(e) => onChange(key, e.target.value)}
            placeholder={placeholder || '{"key": "value"}'}
            disabled={disabled}
            rows={rows}
            className={cn(
              'border-input bg-background w-full resize-none rounded-xl border-2 px-3 py-2 font-mono text-sm',
              'focus-visible:border-primary focus-visible:ring-primary/20 focus-visible:ring-2 focus-visible:outline-none',
              'disabled:cursor-not-allowed disabled:opacity-50',
              'break-all whitespace-pre-wrap',
              error && 'border-destructive',
            )}
          />
        </FieldWrapper>
      )

    case 'select':
      return (
        <FieldWrapper key={String(key)}>
          <select
            value={(value as string) || ''}
            onChange={(e) => onChange(key, e.target.value)}
            disabled={disabled}
            className={cn(
              'border-input bg-background h-9 w-full truncate rounded-xl border-2 px-3 text-sm',
              'focus-visible:border-primary focus-visible:outline-none',
              'disabled:cursor-not-allowed disabled:opacity-50',
              error && 'border-destructive',
            )}
          >
            {placeholder && (
              <option value="" disabled>
                {placeholder}
              </option>
            )}
            {options?.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </FieldWrapper>
      )

    case 'checkbox':
      return (
        <FieldWrapper key={String(key)}>
          <label className="flex cursor-pointer items-start gap-2">
            <input
              type="checkbox"
              checked={Boolean(value)}
              onChange={(e) => onChange(key, e.target.checked)}
              disabled={disabled}
              className="border-input mt-0.5 h-4 w-4 flex-shrink-0 rounded"
            />
            <span className="text-sm break-words">{placeholder || label}</span>
          </label>
        </FieldWrapper>
      )

    default:
      return null
  }
}

/**
 * ConfigModal 组件
 *
 * 提供统一的配置编辑模态框，支持：
 * - 居中定位
 * - 响应式布局
 * - 文本自动换行
 * - 固定底部操作按钮
 * - 平滑过渡动画
 * - ESC 键关闭
 * - 表单验证
 * - localStorage 暂存
 */
export function ConfigModal<T extends object>({
  open,
  onClose,
  onSave,
  title,
  fields,
  config,
  loading = false,
  maxWidth = '2xl',
  configType,
  showReset = true,
  footer,
  className,
  children,
  requireDirty = true,
  submitText = '保存',
}: ConfigModalProps<T>) {
  const modalRef = useRef<HTMLDivElement>(null)

  // 使用表单 Hook
  const {
    data,
    errors,
    isDirty,
    isSubmitting,
    updateField,
    reset,
    validate,
    setSubmitting,
    clearStorage,
  } = useConfigForm(fields, config, configType)

  // ESC 键关闭
  useEffect(() => {
    if (!open) return

    const handleEscape = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose()
      }
    }

    document.addEventListener('keydown', handleEscape)
    return () => document.removeEventListener('keydown', handleEscape)
  }, [open, onClose])

  // 禁止背景滚动
  useEffect(() => {
    if (!open) return

    document.body.style.overflow = 'hidden'
    return () => {
      document.body.style.overflow = ''
    }
  }, [open])

  /**
   * 处理保存
   */
  const handleSave = useCallback(async () => {
    if (!validate()) {
      return
    }

    setSubmitting(true)
    try {
      await onSave(data)
      // 保存成功后清除暂存
      clearStorage()
      onClose()
    } catch (error) {
      console.error('保存配置失败:', error)
    } finally {
      setSubmitting(false)
    }
  }, [data, validate, onSave, clearStorage, onClose, setSubmitting])

  /**
   * 处理重置
   */
  const handleReset = useCallback(() => {
    reset()
  }, [reset])

  /**
   * 点击背景关闭
   */
  const handleBackdropClick = (e: React.MouseEvent) => {
    if (e.target === e.currentTarget) {
      onClose()
    }
  }

  if (!open) return null

  const modal = (
    <div
      ref={modalRef}
      className="fixed inset-0 z-[9999] flex items-center justify-center p-4"
      style={{ zIndex: 9999, position: 'fixed', inset: 0 }}
      onClick={handleBackdropClick}
    >
      {/* 背景遮罩 */}
      <div
        className="absolute inset-0 bg-black/50 backdrop-blur-sm"
        style={{
          position: 'absolute',
          inset: 0,
          backgroundColor: 'rgba(0,0,0,0.5)',
        }}
      />

      {/* 模态框内容 - 添加固定宽度和文本换行 */}
      <div
        className={cn(
          'bg-card text-card-foreground relative rounded-2xl shadow-2xl',
          'flex max-h-[90vh] flex-col',
          'animate-in fade-in-0 zoom-in-95 duration-200',
          'w-full',
          className,
        )}
        style={{
          position: 'relative',
          width: '100%',
          backgroundColor: 'hsl(var(--card))',
          color: 'hsl(var(--foreground))',
          ...maxWidthStyles[maxWidth],
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* 标题栏 - 支持文本换行 */}
        <div className="flex min-w-0 flex-shrink-0 items-center justify-between gap-2 border-b p-6">
          <h2 className="overflow-wrap-anywhere min-w-0 flex-1 text-lg font-semibold break-words whitespace-normal">
            {title}
          </h2>
          <button
            onClick={onClose}
            className="text-muted-foreground hover:text-foreground hover:bg-muted flex-shrink-0 rounded-full p-1 transition-colors"
            aria-label="关闭"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        {/* 内容区 - 可滚动，支持文本换行 */}
        <div className="min-w-0 flex-1 overflow-y-auto p-6">
          {children ? (
            children
          ) : (
            <div className="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2">
              {fields.map((field) =>
                renderField(field, data[field.key], errors[field.key], updateField),
              )}
            </div>
          )}
        </div>

        {/* 底部操作栏 - 固定 */}
        {footer !== undefined ? (
          footer
        ) : (
          <div className="bg-card flex flex-shrink-0 flex-wrap items-center justify-end gap-2 rounded-b-2xl border-t p-6">
            {showReset && (
              <Button
                variant="ghost"
                onClick={handleReset}
                disabled={!isDirty || isSubmitting || loading}
              >
                <RotateCcw className="mr-2 h-4 w-4" />
                重置
              </Button>
            )}
            <Button variant="outline" onClick={onClose} disabled={isSubmitting || loading}>
              取消
            </Button>
            <Button
              onClick={handleSave}
              disabled={(requireDirty && !isDirty) || isSubmitting || loading}
            >
              <Save className="mr-2 h-4 w-4" />
              {isSubmitting || loading ? `${submitText}中...` : submitText}
            </Button>
          </div>
        )}
      </div>
    </div>
  )

  // 使用 Portal 渲染到 body
  return createPortal(modal, document.body)
}

export { useConfigForm } from './useConfigForm'
export * from './types'
