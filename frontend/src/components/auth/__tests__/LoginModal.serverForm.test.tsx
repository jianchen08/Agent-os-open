/** @feature FP-0.2.四 登录前服务器地址表单（ServerAddressForm 交互面） | @ci: frontend-test */
/**
 * LoginModal 次要入口「服务器地址」表单测试（连接切换唯一写点 connectKernel
 * 的委托面，2026-10-23 连接归一）：
 * - 切换开合；非法地址保存拒绝（不触 connectKernel）
 * - 合法地址/留空 → 委托 connectKernel（远端带地址、本机档空串），不自行写存储
 * - connectKernel 失败 → 错误就地呈现（不静默假成功）；成功不重复刷新
 * - 连接进行中防重复提交
 * - 自动登录遇上一发在途（isLoading）让路本轮按间隔重试
 */
import { act, fireEvent, render, screen } from '@testing-library/react'
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
  loadShellLocalCredential: (...args: unknown[]) => shellAuth.loadShellLocalCredential(...args),
  shellAutoLoginDisabled: (...args: unknown[]) => shellAuth.shellAutoLoginDisabled(...args),
}))

const connect = vi.hoisted(() => ({ connectKernel: vi.fn() }))
vi.mock('@/pages/settings/devices/connectKernel', () => ({
  connectKernel: (...args: unknown[]) => connect.connectKernel(...args),
}))

const reload = vi.fn()
Object.defineProperty(window, 'location', {
  value: { ...window.location, reload },
  writable: true,
})

const LOCAL_CRED = { username: 'admin', password: 'device-generated' }

function renderModal() {
  return render(
    <MemoryRouter>
      <LoginModal open onClose={() => {}} />
    </MemoryRouter>,
  )
}

function openServerForm() {
  fireEvent.click(screen.getByTestId('login-server-toggle'))
  return screen.getByTestId('login-server-form')
}

beforeEach(() => {
  localStorage.clear()
  authState.login.mockReset()
  authState.login.mockResolvedValue(undefined)
  authState.isLoading = false
  authState.error = null
  authState.isAuthenticated = false
  shellAuth.loadShellLocalCredential.mockReset()
  shellAuth.loadShellLocalCredential.mockResolvedValue(null)
  shellAuth.shellAutoLoginDisabled.mockReset()
  shellAuth.shellAutoLoginDisabled.mockReturnValue(false)
  connect.connectKernel.mockReset()
  connect.connectKernel.mockResolvedValue({ ok: true })
  reload.mockReset()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('LoginModal 服务器地址表单', () => {
  it('切换按钮开合 ServerAddressForm（默认收起）', () => {
    renderModal()
    expect(screen.queryByTestId('login-server-form')).toBeNull()
    fireEvent.click(screen.getByTestId('login-server-toggle'))
    expect(screen.getByTestId('login-server-form')).toBeInTheDocument()
    fireEvent.click(screen.getByTestId('login-server-toggle'))
    expect(screen.queryByTestId('login-server-form')).toBeNull()
  })

  it('非法地址：保存拒绝（不触 connectKernel）', () => {
    renderModal()
    openServerForm()
    const input = screen.getByTestId('login-server-input')
    fireEvent.change(input!, { target: { value: 'ftp://bad-scheme' } })
    fireEvent.click(screen.getByText('保存并刷新'))
    expect(connect.connectKernel).not.toHaveBeenCalled()
    expect(screen.getByTestId('login-server-error')).toHaveTextContent('仅支持 http/https')
  })

  it('合法地址：委托 connectKernel（远端带地址），不自行写存储', () => {
    renderModal()
    const form = openServerForm()
    fireEvent.change(form.querySelector('input')!, { target: { value: 'http://192.168.1.10:9300' } })
    fireEvent.click(form.querySelector('button')!)
    expect(connect.connectKernel).toHaveBeenCalledWith('http://192.168.1.10:9300')
    expect(localStorage.getItem('server_base_url')).toBeNull()
  })

  it('留空保存：委托 connectKernel 空串（本机档归一）', () => {
    localStorage.setItem('server_base_url', 'http://old:9300')
    renderModal()
    const form = openServerForm()
    fireEvent.change(form.querySelector('input')!, { target: { value: '' } })
    fireEvent.click(form.querySelector('button')!)
    expect(connect.connectKernel).toHaveBeenCalledWith('')
  })

  it('connectKernel 失败（探活不可达/存储不可用）：错误就地呈现，不静默', async () => {
    connect.connectKernel.mockResolvedValue({ ok: false, error: '目标内核不可达（http://dead:9300）' })
    renderModal()
    const form = openServerForm()
    fireEvent.change(form.querySelector('input')!, { target: { value: 'http://dead:9300' } })
    fireEvent.click(form.querySelector('button')!)
    // 假计时器环境：微任务冲刷即完成 promise → setState 链（waitFor 依赖真实定时器，不适用）
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(screen.getByTestId('login-server-save-error')).toHaveTextContent('目标内核不可达')
    expect(reload).not.toHaveBeenCalled()
  })

  it('连接进行中防重复提交：按钮禁用，connectKernel 只触一次', async () => {
    let resolveConnect: (r: { ok: boolean }) => void = () => {}
    connect.connectKernel.mockImplementation(
      () => new Promise<{ ok: boolean }>((resolve) => { resolveConnect = resolve }),
    )
    renderModal()
    const form = openServerForm()
    fireEvent.change(form.querySelector('input')!, { target: { value: 'http://192.168.1.10:9300' } })
    fireEvent.click(form.querySelector('button')!)
    expect(screen.getByText('连接中…')).toBeInTheDocument()
    fireEvent.click(screen.getByText('连接中…'))
    expect(connect.connectKernel).toHaveBeenCalledTimes(1)
    await act(async () => {
      resolveConnect({ ok: true })
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(screen.queryByText('连接中…')).toBeNull()
  })

  it('自动登录让路在途请求：isLoading 时不叠加并发，间隔后重试', async () => {
    shellAuth.loadShellLocalCredential.mockResolvedValue({ ...LOCAL_CRED })
    authState.isLoading = true
    renderModal()
    // 在途让路：本轮不触发 login
    await vi.advanceTimersByTimeAsync(50)
    expect(authState.login).not.toHaveBeenCalled()
    // 上一发完成 → 间隔后重试发起（假计时器推进即同步触发，无需 waitFor 轮询）
    authState.isLoading = false
    await vi.advanceTimersByTimeAsync(5500)
    expect(authState.login).toHaveBeenCalled()
  })
})

describe('登录卡当前目标恢复面（死目标自助回本机，2026-10-26 用户实测）', () => {
  it('自定义目标存在 → 免展开即见当前目标与改用本机内核入口', () => {
    localStorage.setItem('server_base_url', 'http://10.0.2.2:9101')
    renderModal()
    expect(screen.getByTestId('login-current-target-base')).toHaveTextContent(
      'http://10.0.2.2:9101',
    )
    expect(screen.getByTestId('login-use-local-kernel')).toBeInTheDocument()
  })

  it('点改用本机内核 → 委托 connectKernel 空串（本机档同路单一写点）', async () => {
    localStorage.setItem('server_base_url', 'http://10.0.2.2:9101')
    renderModal()
    fireEvent.click(screen.getByTestId('login-use-local-kernel'))
    expect(connect.connectKernel).toHaveBeenCalledWith('')
    expect(screen.getByTestId('login-use-local-kernel')).toBeDisabled()
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(screen.getByText('正在切回本机内核…')).toBeInTheDocument()
  })

  it('本机档（无自定义目标）→ 恢复面不渲染（现状不变）', () => {
    renderModal()
    expect(screen.queryByTestId('login-current-target')).toBeNull()
    expect(screen.queryByTestId('login-use-local-kernel')).toBeNull()
  })

  it('connectKernel 失败 → 错误就地呈现且按钮恢复可重试', async () => {
    localStorage.setItem('server_base_url', 'http://10.0.2.2:9101')
    connect.connectKernel.mockResolvedValue({ ok: false, error: '本地存储不可用，未切换连接' })
    renderModal()
    fireEvent.click(screen.getByTestId('login-use-local-kernel'))
    await act(async () => {
      await Promise.resolve()
      await Promise.resolve()
    })
    expect(screen.getByTestId('login-local-switch-error')).toHaveTextContent(
      '本地存储不可用，未切换连接',
    )
    expect(screen.getByTestId('login-use-local-kernel')).not.toBeDisabled()
  })
})
