/**
 * 认证表单渲染件（登录/注册页同构 JSX 收口，2026-09-16 规则驱动审查 F8）。
 *
 * 单字段（label + Input + 行内校验错误）与页面壳（满屏居中容器 + 标题 +
 * 表单含全局错误横幅与提交按钮 + 底部切换链接）。状态机见 authFormModel.ts。
 */

import { Link } from 'react-router-dom'
import { Button } from '../../components/ui/button'
import { Input } from '../../components/ui/input'
import type { AuthFieldDefinition } from './authFormModel'
import type { FormEvent, ReactNode } from 'react'

/** 单个受控输入字段（a11y 关联齐全：aria-invalid / aria-describedby → 错误节点） */
export function AuthField({
  definition,
  value,
  error,
  disabled,
  onChange,
  onBlur,
}: {
  definition: AuthFieldDefinition
  value: string
  error?: string
  disabled: boolean
  onChange: (value: string) => void
  onBlur: () => void
}) {
  const errorId = `${definition.id}-error`
  return (
    <div className="space-y-2">
      <label htmlFor={definition.id} className="text-foreground block text-sm font-medium">
        {definition.label} <span className="text-destructive">*</span>
      </label>
      <Input
        id={definition.id}
        type={definition.type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onBlur={onBlur}
        placeholder={definition.placeholder}
        disabled={disabled}
        aria-invalid={!!error}
        aria-describedby={error ? errorId : undefined}
        data-testid={definition.inputTestId}
        className={`h-10 min-h-[40px] ${error ? 'border-destructive' : ''}`}
      />
      {error && (
        <p
          id={errorId}
          className="text-destructive min-h-[20px] text-sm"
          data-testid={definition.errorTestId}
        >
          {error}
        </p>
      )}
    </div>
  )
}

/** 认证页壳：满屏居中容器 + 标题 + 表单（全局错误横幅 + 提交按钮）+ 底部切换链接 */
export function AuthPageShell({
  pageTestId,
  title,
  subtitle,
  formLabel,
  formTestId,
  error,
  errorTestId,
  isLoading,
  submitText,
  submitLoadingText,
  submitTestId,
  switchPrompt,
  switchLinkText,
  switchLinkTo,
  switchLinkTestId,
  onSubmit,
  children,
}: {
  pageTestId: string
  title: string
  subtitle: string
  formLabel: string
  formTestId: string
  error: string | null
  errorTestId: string
  isLoading: boolean
  submitText: string
  submitLoadingText: string
  submitTestId: string
  switchPrompt: string
  switchLinkText: string
  switchLinkTo: string
  switchLinkTestId: string
  onSubmit: (e: FormEvent) => void
  children: ReactNode
}) {
  return (
    <div
      className="bg-background text-foreground flex min-h-screen items-center justify-center px-4 py-12"
      data-testid={pageTestId}
    >
      <div className="w-full max-w-md space-y-6">
        {/* 标题 */}
        <div className="space-y-2 text-center">
          <h1 className="text-foreground text-3xl font-bold">{title}</h1>
          <p className="text-muted-foreground">{subtitle}</p>
        </div>

        {/* 表单 */}
        <form onSubmit={onSubmit} className="space-y-5" data-testid={formTestId} role="form" aria-label={formLabel}>
          {/* 全局错误提示 */}
          {error && (
            <div
              className="bg-destructive/10 text-destructive rounded-lg p-3 text-sm"
              data-testid={errorTestId}
            >
              {error}
            </div>
          )}

          {children}

          {/* 提交按钮 */}
          <Button
            type="submit"
            className="mt-2 h-10 w-full"
            disabled={isLoading}
            data-testid={submitTestId}
          >
            {isLoading ? submitLoadingText : submitText}
          </Button>
        </form>

        {/* 页面切换链接 */}
        <p className="text-muted-foreground pt-2 text-center text-sm">
          {switchPrompt}{' '}
          <Link
            to={switchLinkTo}
            className="text-primary font-medium hover:underline"
            data-testid={switchLinkTestId}
          >
            {switchLinkText}
          </Link>
        </p>
      </div>
    </div>
  )
}
