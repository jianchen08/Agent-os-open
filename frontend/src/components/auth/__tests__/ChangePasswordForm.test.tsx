/** @feature FP-0.2.四 认证链加固（D1-3 改密表单 / D1-4 首登强制改密闸） | @ci frontend-test */
/**
 * ChangePasswordForm / ChangePasswordGate 行为测试：
 * - 表单校验（旧口令必填 / 新口令 ≥8 / 两次一致）
 * - 提交调用 changePassword(old, new)，成功出确认提示
 * - 失败显示后端错误（如旧口令错误）
 * - 强制改密闸：渲染提示与退出入口，改密成功出确认提示
 */
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, it, expect, vi, beforeEach } from 'vitest'
const mockChangePassword = vi.fn()
const mockLogout = vi.fn()
const mockStoreState = {
  changePassword: (...args: unknown[]) => mockChangePassword(...(args as [string, string])),
  isLoading: false,
  logout: mockLogout,
}
vi.mock('@/stores/authStore', () => ({
  useAuthStore: (selector?: (s: typeof mockStoreState) => unknown) =>
    selector ? selector(mockStoreState) : mockStoreState,
}))
import { ChangePasswordForm } from '../ChangePasswordForm'
import { ChangePasswordGate } from '../ChangePasswordGate'

async function fillAndSubmit(oldPw: string, newPw: string, confirmPw: string) {
  const user = userEvent.setup()
  if (oldPw !== '') {
    await user.type(screen.getByTestId('change-password-old-input'), oldPw)
  }
  await user.type(screen.getByTestId('change-password-new-input'), newPw)
  await user.type(screen.getByTestId('change-password-confirm-input'), confirmPw)
  await user.click(screen.getByTestId('change-password-submit-button'))
}

describe('ChangePasswordForm（D1-3）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockStoreState.isLoading = false
  })

  it('校验：新口令短于 8 字符提示且不提交', async () => {
    render(<ChangePasswordForm />)
    await fillAndSubmit('old-pass-1', 'short', 'short')
    expect(screen.getByTestId('new-password-error').textContent).toContain('至少为8个字符')
    expect(mockChangePassword).not.toHaveBeenCalled()
  })

  it('校验：两次新口令不一致提示且不提交', async () => {
    render(<ChangePasswordForm />)
    await fillAndSubmit('old-pass-1', 'new-pass-123', 'new-pass-456')
    expect(screen.getByTestId('confirm-password-error').textContent).toContain('不一致')
    expect(mockChangePassword).not.toHaveBeenCalled()
  })

  it('校验：旧口令为空提示且不提交', async () => {
    render(<ChangePasswordForm />)
    await fillAndSubmit('', 'new-pass-123', 'new-pass-123')
    expect(screen.getByTestId('old-password-error').textContent).toContain('旧口令不能为空')
    expect(mockChangePassword).not.toHaveBeenCalled()
  })

  it('提交成功：调用 changePassword(旧, 新) 并显示确认提示', async () => {
    mockChangePassword.mockResolvedValueOnce({
      access_token: 'at-new',
      refresh_token: 'rt-new',
      expires_in: 1800,
    })
    render(<ChangePasswordForm />)
    await fillAndSubmit('old-pass-1', 'new-pass-123', 'new-pass-123')
    expect(mockChangePassword).toHaveBeenCalledWith('old-pass-1', 'new-pass-123')
    expect(screen.getByTestId('change-password-success').textContent).toContain('口令已修改')
  })

  it('提交失败：旧口令错误时显示服务端错误信息', async () => {
    mockChangePassword.mockRejectedValueOnce(new Error('旧口令错误'))
    render(<ChangePasswordForm />)
    await fillAndSubmit('wrong-old', 'new-pass-123', 'new-pass-123')
    expect(screen.getByTestId('change-password-error').textContent).toContain('旧口令错误')
  })
})

describe('ChangePasswordGate（D1-4 首登强制改密）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('渲染强制改密提示、表单与退出入口', () => {
    render(<ChangePasswordGate />)
    expect(screen.getByTestId('change-password-gate').textContent).toContain('请修改初始口令')
    expect(screen.getByTestId('change-password-form')).toBeTruthy()
    expect(screen.getByTestId('change-password-logout-button')).toBeTruthy()
  })

  it('改密成功后显示确认提示（放行由 ProtectedRoute 依标记清除后承接）', async () => {
    mockChangePassword.mockResolvedValueOnce({
      access_token: 'at-new',
      refresh_token: 'rt-new',
      expires_in: 1800,
    })
    render(<ChangePasswordGate />)
    await fillAndSubmit('old-pass-1', 'new-pass-123', 'new-pass-123')
    expect(screen.getByTestId('change-password-success')).toBeTruthy()
  })
})
