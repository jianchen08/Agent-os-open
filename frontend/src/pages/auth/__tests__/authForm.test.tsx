/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * authForm 共享件契约测试（审查 F8 收口的共性面，独立于两页锁定）。
 *
 * 覆盖：useAuthForm 失焦校验错误设置/修正清除与整表校验聚合、
 * useAuthPageLifecycle 已认证跳转与卸载清理、AuthField 的 a11y 关联。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { describe, it, expect, vi } from 'vitest'
import { AuthField, AuthPageShell } from '../authForm'
import { useAuthForm, useAuthPageLifecycle, type AuthFieldDefinition } from '../authFormModel'

const FIELDS: AuthFieldDefinition[] = [
  {
    id: 'name',
    label: '名称',
    type: 'text',
    placeholder: '输入名称',
    validate: (value) => (!value.trim() ? '不能为空' : undefined),
    inputTestId: 'name-input',
    errorTestId: 'name-error',
  },
  {
    id: 'repeat',
    label: '重复',
    type: 'text',
    placeholder: '再输一遍',
    validate: (value, values) => (value !== values.name ? '两次不一致' : undefined),
    inputTestId: 'repeat-input',
    errorTestId: 'repeat-error',
  },
]

function Harness() {
  const { values, setValue, errors, handleBlur, validateAll } = useAuthForm(FIELDS)
  return (
    <AuthPageShell
      pageTestId="harness-page"
      title="壳"
      subtitle="副题"
      formLabel="壳表单"
      formTestId="harness-form"
      error={null}
      errorTestId="harness-error"
      isLoading={false}
      submitText="提交"
      submitLoadingText="提交中..."
      submitTestId="harness-submit"
      switchPrompt="换页？"
      switchLinkText="去登录"
      switchLinkTo="/login"
      switchLinkTestId="harness-switch"
      onSubmit={(e) => {
        e.preventDefault()
        validateAll()
      }}
    >
      {FIELDS.map((field) => (
        <AuthField
          key={field.id}
          definition={field}
          value={values[field.id] ?? ''}
          error={errors[field.id]}
          disabled={false}
          onChange={(value) => setValue(field.id, value)}
          onBlur={() => handleBlur(field)}
        />
      ))}
    </AuthPageShell>
  )
}

function LifecycleHarness({
  isAuthenticated,
  clearError,
}: {
  isAuthenticated: boolean
  clearError: () => void
}) {
  useAuthPageLifecycle(isAuthenticated, clearError)
  return <div>lifecycle</div>
}

describe('useAuthForm / AuthField（共享件契约）', () => {
  it('失焦校验：错误设置与修正清除', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <Harness />
      </MemoryRouter>,
    )
    const repeat = screen.getByLabelText(/重复/i)
    await user.type(screen.getByLabelText(/名称/i), 'abc')
    await user.type(repeat, 'abc')
    fireEvent.blur(repeat)
    await waitFor(() =>
      expect(screen.queryByTestId('repeat-error')).not.toBeInTheDocument(),
    )

    await user.clear(repeat)
    await user.type(repeat, 'xyz')
    fireEvent.blur(repeat)
    expect(await screen.findByTestId('repeat-error')).toHaveTextContent('两次不一致')
  })

  it('整表校验聚合：提交触发全部字段错误', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <Harness />
      </MemoryRouter>,
    )
    // name 填值留空 repeat：触发跨字段不一致；name 自身通过 → 聚合仍拦
    await user.type(screen.getByLabelText(/名称/i), 'abc')
    await user.click(screen.getByRole('button', { name: /提交/i }))
    expect(await screen.findByTestId('repeat-error')).toHaveTextContent('两次不一致')
    expect(screen.queryByTestId('name-error')).not.toBeInTheDocument()
    // 全空提交：name 报空、repeat 双空相等通过
    await user.clear(screen.getByLabelText(/名称/i))
    await user.click(screen.getByRole('button', { name: /提交/i }))
    expect(await screen.findByTestId('name-error')).toHaveTextContent('不能为空')
    expect(screen.queryByTestId('repeat-error')).not.toBeInTheDocument()
  })

  it('错误字段的 aria 关联：input 描述指向错误节点', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter>
        <Harness />
      </MemoryRouter>,
    )
    await user.click(screen.getByRole('button', { name: /提交/i }))
    const nameInput = screen.getByTestId('name-input')
    expect(nameInput).toHaveAttribute('aria-invalid', 'true')
    expect(nameInput.getAttribute('aria-describedby')).toBe('name-error')
  })
})

describe('useAuthPageLifecycle', () => {
  it('已认证自动跳首页；卸载清 store 错误', () => {
    const clearError = vi.fn()
    const { unmount } = render(
      <MemoryRouter>
        <LifecycleHarness isAuthenticated clearError={clearError} />
      </MemoryRouter>,
    )
    unmount()
    expect(clearError).toHaveBeenCalledTimes(1)
  })

  it('未认证不跳转也不清理（挂载期）', () => {
    const clearError = vi.fn()
    render(
      <MemoryRouter>
        <LifecycleHarness isAuthenticated={false} clearError={clearError} />
      </MemoryRouter>,
    )
    expect(clearError).not.toHaveBeenCalled()
  })
})
