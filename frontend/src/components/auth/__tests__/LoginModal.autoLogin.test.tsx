/** @ci: frontend-test */
/**
 * LoginModal 本机免填自动登录（移动壳本机内核档，设计基调 2026-10-15）：
 * - 凭据就绪 → 打开即自动登录（预填+提交）
 * - 内核未就绪（登录失败）→ 按间隔重试至上限（覆盖冷启动 13-40s 窗口）
 * - 用户手动编辑表单 → 停止自动登录（不覆盖手动输入）
 * - 本次运行内登出过（shellAutoLoginDisabled）→ 不自动重登，仅预填
 * - 自定义后端档（无本机凭据）→ 不自动登录（远程必填手输）
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { kernelStageText } from '../kernelStage'
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
  authState.login.mockResolvedValue(undefined)
  authState.isLoading = false
  authState.error = null
  authState.isAuthenticated = false
  shellAuth.loadShellLocalCredential.mockReset()
  shellAuth.shellAutoLoginDisabled.mockReset()
  shellAuth.shellAutoLoginDisabled.mockReturnValue(false)
})

afterEach(() => {
  vi.useRealTimers()
})

describe('LoginModal — 本机免填自动登录', () => {
  it('本机凭据就绪：打开即预填并自动登录', async () => {
    shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
    renderModal()
    await waitFor(() => {
      expect(authState.login).toHaveBeenCalledWith('admin', 'device-generated')
    })
    expect(screen.getByTestId('login-modal-username')).toHaveValue('admin')
    expect(screen.getByTestId('login-modal-password')).toHaveValue('device-generated')
  })

  it('内核未就绪（登录失败）：持续重试至 10 分钟长窗，超窗落内核终态文案', async () => {
    vi.useFakeTimers()
    shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
    authState.login.mockRejectedValue(new Error('connection refused'))
    renderModal()
    await vi.advanceTimersByTimeAsync(0)
    expect(authState.login).toHaveBeenCalledTimes(1)
    // 60s 处仍在重试（旧 12 次窗已废：真机首启装配可超 60s，用户 2026-10-26 实证）
    await vi.advanceTimersByTimeAsync(60_000)
    expect(authState.login).toHaveBeenCalledTimes(13)
    // 10 分钟窗内继续
    await vi.advanceTimersByTimeAsync(120_000)
    expect(authState.login).toHaveBeenCalledTimes(37)
    // 超窗：链停 + 内核措辞终态（非「无法连接」远程措辞）
    await vi.advanceTimersByTimeAsync(600_000)
    const finalCalls = authState.login.mock.calls.length
    await vi.advanceTimersByTimeAsync(60_000)
    expect(authState.login).toHaveBeenCalledTimes(finalCalls)
    expect(screen.getByTestId('login-kernel-giveup')).toHaveTextContent('本机内核未能启动')
    expect(screen.getByTestId('login-kernel-giveup')).toHaveTextContent('重启应用')
    expect(screen.queryByTestId('login-modal-error')).toBeNull()
  })

  it('等待期间压制连接错误：呈现内核状态行而非「无法连接内核」', async () => {
    vi.useFakeTimers()
    shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
    authState.login.mockRejectedValue(new Error('connection refused'))
    authState.error = '无法连接内核（目标：http://127.0.0.1:9300），请确认服务已启动或检查服务器地址'
    renderModal()
    await vi.advanceTimersByTimeAsync(0)
    await vi.advanceTimersByTimeAsync(5_000)
    expect(screen.getByTestId('login-kernel-status')).toHaveTextContent('本机内核启动中')
    expect(screen.getByTestId('login-kernel-status')).toHaveTextContent('已等待')
    expect(screen.queryByTestId('login-modal-error')).toBeNull()
  })

  it('连接前探内核状态（壳桥）：未 running 不空打登录，running 才发登录', async () => {
    vi.useFakeTimers()
    shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
    let running = false
    ;(window as unknown as { Capacitor?: unknown }).Capacitor = {
      Plugins: {
        KernelHost: {
          status: vi.fn(async () => ({ running, port: '9300', seeded: true, initialized: true })),
        },
      },
    }
    renderModal()
    await vi.advanceTimersByTimeAsync(0)
    // 内核未起：只探状态，不空打登录
    await vi.advanceTimersByTimeAsync(11_000)
    expect(authState.login).not.toHaveBeenCalled()
    expect(screen.getByTestId('login-kernel-status')).toHaveTextContent('本机内核启动中')
    // 内核起来了：下一轮探到 running 即发登录
    running = true
    await vi.advanceTimersByTimeAsync(6_000)
    expect(authState.login).toHaveBeenCalledWith('admin', 'device-generated')
    ;(window as unknown as { Capacitor?: unknown }).Capacitor = undefined
  })

  it('原生启动失败面：boot_error 即时呈现具体原因，不傻等超时窗', async () => {
    vi.useFakeTimers()
    shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
    ;(window as unknown as { Capacitor?: unknown }).Capacitor = {
      Plugins: {
        KernelHost: {
          status: vi.fn(async () => ({
            running: false,
            seeded: true,
            initialized: false,
            stage: 'assemble',
            boot_error: 'mobile_init exit 1',
            boot_error_stage: 'assemble',
          })),
        },
      },
    }
    renderModal()
    await vi.advanceTimersByTimeAsync(6_000)
    expect(screen.getByTestId('login-kernel-giveup')).toHaveTextContent('内核启动失败')
    expect(screen.getByTestId('login-kernel-giveup')).toHaveTextContent('环境装配阶段')
    expect(screen.getByTestId('login-kernel-giveup')).toHaveTextContent('mobile_init exit 1')
    expect(authState.login).not.toHaveBeenCalled()
    ;(window as unknown as { Capacitor?: unknown }).Capacitor = undefined
  })

  it('BootProgress 进度面透传：装配计数进状态行', async () => {
    vi.useFakeTimers()
    shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
    ;(window as unknown as { Capacitor?: unknown }).Capacitor = {
      Plugins: {
        KernelHost: {
          status: vi.fn(async () => ({
            running: false,
            seeded: true,
            initialized: false,
            stage: 'assemble',
            extract_entries: 160000,
            venv_done: 5,
          })),
        },
      },
    }
    renderModal()
    await vi.advanceTimersByTimeAsync(6_000)
    expect(screen.getByTestId('login-kernel-status')).toHaveTextContent('本机环境装配中')
    expect(screen.getByTestId('login-kernel-status')).toHaveTextContent('已完成 5 个模块')
    ;(window as unknown as { Capacitor?: unknown }).Capacitor = undefined
  })

  it('429 限速锁（终态非可重试态）：链即停，不再继续打', async () => {
    vi.useFakeTimers()
    shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
    // 内核限速锁形状（auth.rs TooManyRequests：机器码 TOO_MANY_REQUESTS + Retry-After）
    authState.login.mockRejectedValue(
      Object.assign(new Error('登录失败次数过多，请 900 秒后重试'), {
        code: 'TOO_MANY_REQUESTS',
        retry_after_secs: 900,
      }),
    )
    renderModal()
    await vi.advanceTimersByTimeAsync(0)
    expect(authState.login).toHaveBeenCalledTimes(1)
    // 锁定期内后续轮次全部不再发起（红：现状 429 继续打满 12 次）
    await vi.advanceTimersByTimeAsync(60_000)
    expect(authState.login).toHaveBeenCalledTimes(1)
  })

  it('凭据错（漂移态，401 族）：维持既有链语义继续重试（壳层重播种接手自愈）', async () => {
    vi.useFakeTimers()
    shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
    authState.login.mockRejectedValue(
      Object.assign(new Error('用户名或密码错误'), { code: 'BAD_REQUEST' }),
    )
    renderModal()
    await vi.advanceTimersByTimeAsync(0)
    expect(authState.login).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(55_000)
    expect(authState.login).toHaveBeenCalledTimes(12)
  })

  it('登录成功（认证态翻转）：停止重试链', async () => {
    vi.useFakeTimers()
    shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
    const { rerender } = renderModal()
    await vi.advanceTimersByTimeAsync(0)
    expect(authState.login).toHaveBeenCalledTimes(1)
    authState.isAuthenticated = true
    rerender(
      <MemoryRouter>
        <LoginModal open onClose={() => {}} />
      </MemoryRouter>,
    )
    await vi.advanceTimersByTimeAsync(60_000)
    expect(authState.login).toHaveBeenCalledTimes(1)
  })

  it('用户手动编辑表单：自动登录停止，不覆盖手动输入', async () => {
    vi.useFakeTimers()
    shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
    authState.login.mockRejectedValue(new Error('kernel not ready'))
    renderModal()
    await vi.advanceTimersByTimeAsync(0)
    expect(authState.login).toHaveBeenCalledTimes(1)
    fireEvent.change(screen.getByTestId('login-modal-username'), {
      target: { value: 'manual-user' },
    })
    await vi.advanceTimersByTimeAsync(60_000)
    expect(authState.login).toHaveBeenCalledTimes(1)
    expect(screen.getByTestId('login-modal-username')).toHaveValue('manual-user')
    // 编辑后等待态让位（不再呈现「启动中」状态行）
    expect(screen.queryByTestId('login-kernel-status')).toBeNull()
  })

  it('本次运行内登出过（旗标）：不自动重登，仅预填', async () => {
    shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
    shellAuth.shellAutoLoginDisabled.mockReturnValue(true)
    renderModal()
    await waitFor(() => {
      expect(screen.getByTestId('login-modal-username')).toHaveValue('admin')
    })
    expect(authState.login).not.toHaveBeenCalled()
  })

  it('自定义后端档（无本机凭据）：不预填不自动登录（远程必填）', async () => {
    shellAuth.loadShellLocalCredential.mockResolvedValue(null)
    renderModal()
    await waitFor(() => {
      expect(shellAuth.loadShellLocalCredential).toHaveBeenCalled()
    })
    expect(authState.login).not.toHaveBeenCalled()
    expect(screen.getByTestId('login-modal-username')).toHaveValue('')
  })
})

describe('LoginModal — 桌面装机档免填自动登录（env 事实源，2026-11-08）', () => {
  const DESKTOP_CRED = { username: 'admin', password: 'env-seeded-hex' }

  function setDesktopCredential(cred: { username: string; password: string } | null): void {
    ;(window as unknown as { electronAPI?: unknown }).electronAPI = cred
      ? { adminCredential: { load: vi.fn(async () => cred) } }
      : undefined
  }

  afterEach(() => {
    ;(window as unknown as { electronAPI?: unknown }).electronAPI = undefined
  })

  it('桌面凭据就绪（无壳桥）：打开即预填并自动登录（盲试模式）', async () => {
    shellAuth.loadShellLocalCredential.mockResolvedValue(null)
    setDesktopCredential(DESKTOP_CRED)
    renderModal()
    await waitFor(() => {
      expect(authState.login).toHaveBeenCalledWith('admin', 'env-seeded-hex')
    })
    expect(screen.getByTestId('login-modal-username')).toHaveValue('admin')
    expect(screen.getByTestId('login-modal-password')).toHaveValue('env-seeded-hex')
  })

  it('桌面内核未就绪：持续重试至内核可用（竞速窗自愈——修复单发失败钉死登录页）', async () => {
    vi.useFakeTimers()
    shellAuth.loadShellLocalCredential.mockResolvedValue(null)
    setDesktopCredential(DESKTOP_CRED)
    authState.login.mockRejectedValueOnce(new Error('connection refused'))
    const { rerender } = renderModal()
    await vi.advanceTimersByTimeAsync(0)
    expect(authState.login).toHaveBeenCalledTimes(1)
    // 重试窗内第二发成功（内核就绪）→ 认证态翻转 → 链停（不再空打）
    await vi.advanceTimersByTimeAsync(5_000)
    expect(authState.login).toHaveBeenCalledTimes(2)
    authState.isAuthenticated = true
    rerender(
      <MemoryRouter>
        <LoginModal open onClose={() => {}} />
      </MemoryRouter>,
    )
    await vi.advanceTimersByTimeAsync(60_000)
    expect(authState.login).toHaveBeenCalledTimes(2)
  })

  it('桌面档壳凭据优先：两者同时在位用壳凭据（移动壳语义不被侵蚀）', async () => {
    vi.useFakeTimers()
    shellAuth.loadShellLocalCredential.mockResolvedValue(LOCAL_CRED)
    setDesktopCredential(DESKTOP_CRED)
    renderModal()
    await vi.advanceTimersByTimeAsync(0)
    expect(authState.login).toHaveBeenCalledWith('admin', 'device-generated')
  })

  it('桌面档登出旗标：不自动登录', async () => {
    shellAuth.loadShellLocalCredential.mockResolvedValue(null)
    setDesktopCredential(DESKTOP_CRED)
    shellAuth.shellAutoLoginDisabled.mockReturnValue(true)
    renderModal()
    await waitFor(() => {
      expect(screen.getByTestId('login-modal-username')).toHaveValue('admin')
    })
    expect(authState.login).not.toHaveBeenCalled()
  })

  it('桌面档自定义后端（非回环）：凭据不取不用（对方实例必手输）', async () => {
    shellAuth.loadShellLocalCredential.mockResolvedValue(null)
    setDesktopCredential(DESKTOP_CRED)
    localStorage.setItem('server_base_url', 'http://192.168.1.10:9300')
    renderModal()
    await waitFor(() => {
      expect(shellAuth.loadShellLocalCredential).toHaveBeenCalled()
    })
    expect(authState.login).not.toHaveBeenCalled()
    expect(screen.getByTestId('login-modal-username')).toHaveValue('')
    localStorage.removeItem('server_base_url')
  })

  it('桌面档回环自填（127.0.0.1）：豁免=本机内核，自动登录照走', async () => {
    vi.useFakeTimers()
    shellAuth.loadShellLocalCredential.mockResolvedValue(null)
    setDesktopCredential(DESKTOP_CRED)
    localStorage.setItem('server_base_url', 'http://127.0.0.1:9101')
    renderModal()
    await vi.advanceTimersByTimeAsync(0)
    expect(authState.login).toHaveBeenCalledWith('admin', 'env-seeded-hex')
    localStorage.removeItem('server_base_url')
  })

  it('dev/浏览器（无 electronAPI 桩）：不自动登录（回落手输）', async () => {
    shellAuth.loadShellLocalCredential.mockResolvedValue(null)
    setDesktopCredential(null)
    renderModal()
    await waitFor(() => {
      expect(shellAuth.loadShellLocalCredential).toHaveBeenCalled()
    })
    expect(authState.login).not.toHaveBeenCalled()
    expect(screen.getByTestId('login-modal-username')).toHaveValue('')
  })
})

describe('kernelStageText 阶段文案映射', () => {
  it('解压阶段带真实条目计数', () => {
    expect(kernelStageText({ running: false, stage: 'extract', extract_entries: 160000 })).toBe(
      '本机内核资源解压中（已解压 160000 个文件）',
    )
    expect(kernelStageText({ running: false, stage: 'extract', extract_entries: 0 })).toBe(
      '本机内核资源解压中',
    )
  })

  it('装配阶段带模块计数；拉起/旧包布尔面退化映射', () => {
    expect(kernelStageText({ running: false, stage: 'assemble', venv_done: 12 })).toBe(
      '本机环境装配中（已完成 12 个模块）',
    )
    expect(
      kernelStageText({ running: false, seeded: true, initialized: true, stage: 'starting' }),
    ).toBe('本机内核启动中')
    // 旧包无 stage：按布尔面推断
    expect(kernelStageText({ running: false, seeded: false, initialized: false })).toBe(
      '本机内核资源解压中',
    )
    expect(kernelStageText(null)).toBe('本机内核启动中')
    expect(kernelStageText({ running: true })).toBe('本机内核已就绪，登录中')
  })
})
