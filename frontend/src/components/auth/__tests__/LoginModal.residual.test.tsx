/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * LoginModal 免填自动登录链的残余分支：
 * - 上一发仍在路上（isLoading）→ 本轮让路不叠加并发登录，间隔后重试
 * - 用户手动编辑后链挂起（不再轮询），等待秒数越过硬上限 → 强制裁断等待态
 *   并落「本机内核未能启动」终态（放弃机制不可被绕过的兜底）
 *
 * mock 边界：authStore（登录网络动作）、壳本地凭据桥（原生壳能力）。计时全走
 * fake timers（等待秒数/重试间隔）。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LoginModal } from '../LoginModal'

const authState = vi.hoisted(() => ({
  login: vi.fn(),
  isLoading: false,
  error: null as string | null,
  isAuthenticated: false,
  clearError: vi.fn(),
}))

vi.mock('@/stores/authStore', () => ({
  useAuthStore: Object.assign(() => authState, { getState: () => authState }),
}))

const shellAuth = vi.hoisted(() => ({
  loadShellLocalCredential: vi.fn(),
  shellAutoLoginDisabled: vi.fn((): boolean => false),
}))

vi.mock('@/services/shellLocalAuth', () => ({
  loadShellLocalCredential: (...args: unknown[]) =>
    shellAuth.loadShellLocalCredential(...args),
  shellAutoLoginDisabled: (...args: unknown[]) => shellAuth.shellAutoLoginDisabled(...args),
}))

// 壳桥内核状态（原生能力）：默认无桥（null）＝退化盲试；链超时用例给慢应答桥
const shellHost = vi.hoisted(() => ({ host: null as null | { status: () => Promise<unknown> } }))
vi.mock('@/services/shellPlugins', () => ({
  getShellKernelHost: () => shellHost.host,
}))

const LOCAL_CRED = { username: 'admin', password: 'device-generated' }

function renderModal() {
  return render(
    <MemoryRouter>
      <LoginModal open onClose={() => {}} />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  authState.login.mockReset()
  authState.login.mockRejectedValue(new Error('connection refused'))
  authState.isLoading = false
  authState.error = null
  authState.isAuthenticated = false
  shellAuth.loadShellLocalCredential.mockReset()
  shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
  shellAuth.shellAutoLoginDisabled.mockReset()
  shellAuth.shellAutoLoginDisabled.mockReturnValue(false)
  shellHost.host = null
})

afterEach(() => {
  vi.useRealTimers()
})

describe('LoginModal — 免填链的并发让路', () => {
  it('上一发仍在路上 → 本轮不发登录，间隔后自动重试', async () => {
    vi.useFakeTimers()
    authState.isLoading = true
    renderModal()
    await vi.advanceTimersByTimeAsync(0)

    expect(authState.login).not.toHaveBeenCalled()

    // 上一发结束（loading 落下）→ 下一个重试间隔到达时正常发起
    authState.isLoading = false
    await vi.advanceTimersByTimeAsync(5_000)

    expect(authState.login).toHaveBeenCalledTimes(1)
  })
})

describe('LoginModal — 等待态硬上限兜底', () => {
  it('用户手动编辑后链挂起，秒数越过上限+20s → 强制裁断并落内核终态文案', async () => {
    vi.useFakeTimers()
    renderModal()
    await vi.advanceTimersByTimeAsync(0)
    expect(authState.login).toHaveBeenCalledTimes(1)

    // 用户手动编辑：链在下一轮让位（不再重试），等待态仍在
    fireEvent.change(screen.getByTestId('login-modal-username'), {
      target: { value: '手动输入' },
    })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(authState.login).toHaveBeenCalledTimes(1)

    // 越过硬上限（600s + 20s 余量）：渲染层兜底裁断等待态
    await vi.advanceTimersByTimeAsync(630_000)

    expect(screen.getByTestId('login-kernel-giveup')).toHaveTextContent('本机内核未能启动')
    expect(screen.queryByTestId('login-kernel-status')).not.toBeInTheDocument()
  })
})

describe('LoginModal — 免填链自身超窗', () => {
  it('壳桥应答慢使轮次内越过 10 分钟窗 → 链在登录前落内核终态文案（不再空打登录）', async () => {
    vi.useFakeTimers()
    let running = true
    // 壳桥内核状态慢应答（4.5s，低于 5s 探活超时）：时间在轮次内前移
    shellHost.host = {
      status: () =>
        new Promise((resolve) => {
          setTimeout(() => resolve({ running }), 4_500)
        }),
    }
    renderModal()
    await vi.advanceTimersByTimeAsync(4_500)
    expect(authState.login).toHaveBeenCalledTimes(1)

    // 逐轮推进（轮内 4.5s 探活 + 5s 重试间隔），直至链自身越过 10 分钟窗
    for (let i = 0; i < 70; i += 1) {
      await vi.advanceTimersByTimeAsync(4_500)
      await vi.advanceTimersByTimeAsync(5_000)
      if (screen.queryByTestId('login-kernel-giveup')) break
    }

    expect(screen.getByTestId('login-kernel-giveup')).toHaveTextContent('本机内核未能启动')
    // 终态后不再继续轮询登录
    running = true
    const callsAtGiveUp = authState.login.mock.calls.length
    await vi.advanceTimersByTimeAsync(20_000)
    expect(authState.login.mock.calls.length).toBe(callsAtGiveUp)
  }, 20_000)
})
