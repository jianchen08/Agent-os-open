/**
 * LoginModal 弹窗式登录框
 *
 * 在当前页弹出登录表单(不跳转 /login),复用 useAuthStore.login。
 * 用于侧边栏用户区:未登录点击 → 弹出本框;已登录可在用户菜单里"切换账号"再次弹出。
 * 亦是全局认证闸（AuthGate，dismissible=false 档）：未认证时不可关闭，
 * 登录成功后由 ProtectedRoute 放行目标页（ADR 2026-09-28）。
 */

import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { ROUTES } from '@/constants/routes'
import { connectKernel } from '@/pages/settings/devices/connectKernel'
import { classifyLoginFailure } from '@/services/api/loginFailure'
import { validateServerBaseUrl } from '@/services/connectionProbe'
import { getServerBaseUrl } from '@/services/shellApiBase'
import {
  loadShellLocalCredential,
  shellAutoLoginDisabled,
  type ShellLocalCredential,
} from '@/services/shellLocalAuth'
import { getShellKernelHost } from '@/services/shellPlugins'
import { useAuthStore } from '@/stores/authStore'
import { bootStageLabel, kernelStageText, type ShellKernelStatus } from './kernelStage'

/**
 * 本机免填自动登录（移动壳本机内核档）参数：内核冷启动真机可达数分钟（解压+
 * 17 venv 装配+拉起；2026-10-26 用户真机实证 60s 窗停链后错误钉死登录页）——
 * 5s 间隔持续等待至 10 分钟长窗，窗口内不呈现连接错误（连接前先探壳桥
 * KernelHost.status() 真实状态，未 running 不空打登录）；超窗才落「内核未能
 * 启动」终态文案。429 限速锁与手动编辑仍是终态停链。
 */
const AUTO_LOGIN_RETRY_INTERVAL_MS = 5000
const AUTO_LOGIN_MAX_WAIT_MS = 600_000
/** 壳桥探活超时：status() 挂死不归时按未 running 处理（放弃机制不可被卡死） */
const STATUS_PROBE_TIMEOUT_MS = 5000

interface FormErrors {
  username?: string
  password?: string
}

/**
 * 登录卡「服务器」次要入口：内联表单改连接目标（连接切换唯一写点
 * connectKernel——写 server_base_url + 上报后端模式 + 整页刷新收尾，
 * 见 pages/settings/devices/connectKernel）。远端目标保存前探活闸同闸：
 * 不可达拒绝切换并就地呈现错误。API 基址是模块加载期常量，刷新后新地址
 * 才在本次登录生效。校验与连接设置页同源（validateServerBaseUrl）。
 */
function ServerAddressForm() {
  const [value, setValue] = useState(() => getServerBaseUrl() ?? '')
  const validation = validateServerBaseUrl(value)
  /** connectKernel 失败文案（探活不可达/存储不可用），就地呈现不静默 */
  const [saveError, setSaveError] = useState<string | null>(null)
  /** 连接切换进行中（远端目标探活异步窗口），防重复提交 */
  const [connecting, setConnecting] = useState(false)

  const handleSave = () => {
    if (!validation.ok || connecting) return
    setSaveError(null)
    setConnecting(true)
    void connectKernel(validation.url ?? '').then((result) => {
      setConnecting(false)
      if (!result.ok) setSaveError(result.error)
      // ok：connectKernel 已定时刷新收尾（本组件不重复刷新）
    })
  }

  return (
    <div className="space-y-2" data-testid="login-server-form">
      <Input
        type="text"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder="服务器地址，如 http://192.168.1.10:9300（留空用默认）"
        className="h-9 font-mono text-xs"
        data-testid="login-server-input"
      />
      {!validation.ok && (
        <p className="text-destructive text-xs" data-testid="login-server-error">
          {validation.reason}
        </p>
      )}
      {validation.ok && saveError && (
        <p className="text-destructive text-xs" data-testid="login-server-save-error">
          {saveError}
        </p>
      )}
      <Button
        type="button"
        variant="outline"
        className="h-8 w-full text-xs"
        onClick={handleSave}
        disabled={!validation.ok || connecting}
      >
        {connecting ? '连接中…' : '保存并刷新'}
      </Button>
      <p className="text-muted-foreground text-center text-xs">
        留空则使用应用内置服务器地址
      </p>
    </div>
  )
}

export interface LoginModalProps {
  /** 是否显示 */
  open: boolean
  /** 关闭回调 */
  onClose: () => void
  /** 是否允许用户主动关闭（X/Esc）；认证闸档传 false（默认 true） */
  dismissible?: boolean
}

/** 弹窗式登录框 */
export function LoginModal({ open, onClose, dismissible = true }: LoginModalProps) {
  const { login, isLoading, error, isAuthenticated, clearError } = useAuthStore()

  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [formErrors, setFormErrors] = useState<FormErrors>({})
  /** 服务器地址内联表单开关（登录卡次要入口） */
  const [serverOpen, setServerOpen] = useState(false)
  /** 当前自定义连接目标（null=本机档；非 null 时呈现恢复面，挂载期读一次） */
  const [currentTarget] = useState(() => getServerBaseUrl())
  /** 改用本机内核进行中（connectKernel 成功后整页刷新收尾） */
  const [switchingLocal, setSwitchingLocal] = useState(false)
  /** 改用本机内核失败文案（就地呈现，可重试） */
  const [localSwitchError, setLocalSwitchError] = useState<string | null>(null)
  /** 本机内核档凭据（壳设备侧生成；非本机档/无壳为 null，远程必填手输） */
  const [localCredential, setLocalCredential] = useState<ShellLocalCredential | null>(null)
  /** 免填链等待态：真=链在飞，呈现内核状态行并压制连接错误（用户令 2026-10-26） */
  const [autoWaiting, setAutoWaiting] = useState(false)
  const [waitingSecs, setWaitingSecs] = useState(0)
  /** 壳桥上报的内核真实状态（连接前探面；null=桥不可用退化盲试） */
  const [kernelStatus, setKernelStatus] = useState<ShellKernelStatus | null>(null)
  /** 长窗等待超时的内核措辞终态（替代「无法连接」远程措辞） */
  const [kernelGiveUp, setKernelGiveUp] = useState<string | null>(null)
  /** 用户手动编辑过表单：停止自动登录（不覆盖手动输入） */
  const manualEditRef = useRef(false)
  /** 预填一次性标记（凭据只在首次打开时填入，不回写手动清空的字段） */
  const didPrefillRef = useRef(false)

  const resetAndClose = () => {
    setUsername('')
    setPassword('')
    setFormErrors({})
    clearError()
    manualEditRef.current = false
    didPrefillRef.current = false
    onClose()
  }

  /** 死目标自助恢复（2026-10-26 用户实测：残留自定义地址把应用钉在登录页）：
   * 改用本机内核 = connectKernel('') 本机档同路（单一连接写点），刷新后由
   * 壳免填自动登录接手，全程零手输。失败就地呈现不假成功。 */
  const switchToLocalKernel = () => {
    if (switchingLocal) return
    setLocalSwitchError(null)
    setSwitchingLocal(true)
    void connectKernel('').then((result) => {
      if (!result.ok) {
        setSwitchingLocal(false)
        setLocalSwitchError(result.error)
      }
    })
  }

  // 登录成功 → 自动关闭弹窗并清空表单。
  // 关键:只在"登录态从 false → true"(真正的登录动作)时关闭,
  // 已登录态打开弹窗(切换账号)时不触发关闭,否则一打开就被关掉。
  const wasAuthenticatedRef = useRef(false)
  useEffect(() => {
    if (open && isAuthenticated && !wasAuthenticatedRef.current) {
      resetAndClose()
    }
    wasAuthenticatedRef.current = isAuthenticated
    // HACK: resetAndClose 是组件内闭包（非稳定引用），列入依赖会每次渲染
    // 重建 effect；本 effect 只关心登录态边沿，闭包读最新 state 即可。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isAuthenticated, open])

  useEffect(() => {
    if (open) {
      clearError()
    }
    // HACK: 只在弹窗开合边沿清一次 store 错误；clearError 为 zustand 稳定
    // action（store 创建期绑定），无需入依赖（入列亦无碍，从简）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])

  // 本机内核档凭据加载：弹窗打开即取一次（异步；桥失败/非本机档 → null 回落手输）
  useEffect(() => {
    if (!open) return
    let cancelled = false
    void loadShellLocalCredential().then((cred) => {
      if (!cancelled) setLocalCredential(cred)
    })
    return () => {
      cancelled = true
    }
  }, [open])

  // 免填预填：凭据就绪且字段为空时填入一次（不回写用户手动清空的字段）
  useEffect(() => {
    if (!open || !localCredential || didPrefillRef.current) return
    didPrefillRef.current = true
    setUsername((u) => (u ? u : localCredential.username))
    setPassword((p) => (p ? p : localCredential.password))
  }, [open, localCredential])

  // 本机内核档免填自动登录：连接前先探壳桥 KernelHost.status() 真实状态——
  // 未 running 不空打登录（连接错误不再出现），状态行如实呈现启动阶段；内核
  // running 才发登录，成功由 isAuthenticated 边沿关窗。10 分钟长窗：真机首启
  // 解压+装配可远超 60s，窗内持续等待；超窗落「内核未能启动」终态。429 限速
  // 锁与手动编辑仍是终态停链（登出旗标 shellAutoLoginDisabled 不进链）。
  useEffect(() => {
    if (!open || !localCredential || isAuthenticated) return
    if (shellAutoLoginDisabled()) return
    const host = getShellKernelHost()
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    let rateLimited = false
    const startTs = Date.now()
    setAutoWaiting(true)
    setKernelGiveUp(null)
    setKernelStatus(null)

    const giveUp = (): void => {
      setAutoWaiting(false)
      setKernelGiveUp(
        `本机内核未能启动（已等待 ${Math.round((Date.now() - startTs) / 1000)} 秒）——` +
          '请重启应用再试一次；多次失败请反馈内核日志',
      )
    }

    const attempt = async (): Promise<void> => {
      if (cancelled || manualEditRef.current || rateLimited) return
      if (Date.now() - startTs >= AUTO_LOGIN_MAX_WAIT_MS) {
        giveUp()
        return
      }
      if (useAuthStore.getState().isLoading) {
        // 上一发还在路上（手动提交或上轮重试）：让路本轮，不叠加并发登录
        timer = setTimeout(() => void pollAndAttempt(), AUTO_LOGIN_RETRY_INTERVAL_MS)
        return
      }
      try {
        await login(localCredential.username, localCredential.password)
      } catch (err) {
        if (classifyLoginFailure(err).kind === 'rate_limited') {
          rateLimited = true
          setAutoWaiting(false)
          return
        }
        // 网络类失败不断链：内核可能刚就绪还在预热，下一轮再来（错误被等待态压制）
      }
      if (cancelled) return
      if (!rateLimited) {
        timer = setTimeout(() => void pollAndAttempt(), AUTO_LOGIN_RETRY_INTERVAL_MS)
      }
    }

    const pollAndAttempt = async (): Promise<void> => {
      if (cancelled || manualEditRef.current || rateLimited) return
      if (Date.now() - startTs >= AUTO_LOGIN_MAX_WAIT_MS) {
        giveUp()
        return
      }
      if (host) {
        try {
          // 探活挂死防护（用户真机 716s 实证链可被挂起）：桥调用 5s 不归即按
          // 未 running 处理继续轮转，绝不无限 await 卡死放弃机制
          const st = (await Promise.race([
            host.status() as Promise<ShellKernelStatus>,
            new Promise<null>((resolve) => setTimeout(() => resolve(null), STATUS_PROBE_TIMEOUT_MS)),
          ])) as ShellKernelStatus | null
          if (cancelled) return
          if (st) {
            setKernelStatus(st)
            // 启动失败面（用户令：启动有任何问题都要显示出来）：原生任一阶段
            // 失败即即时呈现具体原因，不再等超时窗
            if (st.boot_error) {
              setAutoWaiting(false)
              setKernelGiveUp(
                `内核启动失败（${bootStageLabel(st.boot_error_stage)}阶段）：${st.boot_error} —— 重启应用可重试`,
              )
              return
            }
            if (st.running !== true) {
              // 内核未起：不空打登录（连接错误面即此根除），等下一轮再探
              timer = setTimeout(() => void pollAndAttempt(), AUTO_LOGIN_RETRY_INTERVAL_MS)
              return
            }
          }
          // st=null（探活超时）：退化盲试语义，落到 attempt
        } catch {
          // 桥抖动/旧包无桩：退化回既有盲试语义
        }
      }
      await attempt()
    }

    void pollAndAttempt()
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
      setAutoWaiting(false)
    }
    // HACK: 链只随「打开/凭据/认证态」边沿重建；login 等闭包读最新 store 即可
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, localCredential, isAuthenticated])

  // 等待秒数走表（状态行「已等待 X 秒」），链终态即停
  useEffect(() => {
    if (!autoWaiting) return
    setWaitingSecs(0)
    const ticker = setInterval(() => setWaitingSecs((s) => s + 1), 1000)
    return () => clearInterval(ticker)
  }, [autoWaiting])

  // 等待态硬上限（渲染层兜底，用户真机 716s 实证链可能被挂起）：链自身超时
  // 失效时，秒数越过上限+20s 仍强制裁断等待态——放弃机制不允许被绕过
  useEffect(() => {
    if (!autoWaiting || waitingSecs < AUTO_LOGIN_MAX_WAIT_MS / 1000 + 20) return
    setAutoWaiting(false)
    setKernelGiveUp(
      `本机内核未能启动（已等待 ${Math.floor(AUTO_LOGIN_MAX_WAIT_MS / 1000)} 秒）——` +
        '请重启应用再试一次；多次失败请反馈内核日志',
    )
  }, [autoWaiting, waitingSecs])

  const handleDismiss = () => {
    // 认证闸档不可主动关闭（未认证时关闭即无可用界面）
    if (!dismissible) return
    resetAndClose()
  }

  const validateField = (field: keyof FormErrors): string | undefined => {
    switch (field) {
      case 'username':
        return !username.trim() ? '用户名不能为空' : undefined
      case 'password':
        return !password ? '密码不能为空' : undefined
      default:
        return undefined
    }
  }

  const validateForm = (): boolean => {
    const errors: FormErrors = {}
    const usernameError = validateField('username')
    if (usernameError) errors.username = usernameError
    const passwordError = validateField('password')
    if (passwordError) errors.password = passwordError
    setFormErrors(errors)
    return Object.keys(errors).length === 0
  }

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!validateForm()) return
    try {
      await login(username.trim(), password)
      // 登录成功由 useEffect 监听 isAuthenticated 关闭弹窗
    } catch {
      // 错误已在 store 中处理,弹窗内显示
    }
  }

  return (
    // 原 ui/Modal（maxWidth="sm"）迁移：width=24rem 等价 max-w-sm 面板宽度；
    // DialogContent 已内建「外点不关」（对齐 Modal closeOnBackdropClick 缺省 false）
    <Dialog
      open={open}
      onOpenChange={(o) => {
        if (!o) handleDismiss()
      }}
    >
      <DialogContent width="24rem">
        <DialogHeader className="border-b pb-6" showClose={dismissible}>
          <DialogTitle>登录</DialogTitle>
        </DialogHeader>
        <div className="p-6">
          <form onSubmit={handleSubmit} className="space-y-4" data-testid="login-modal-form">
            {/* 本机档免填链等待态：内核状态行（解压/装配/拉起如实呈现），连接
                错误被压制——内核没起来不是「连不上」（用户令 2026-10-26） */}
            {autoWaiting && !manualEditRef.current ? (
              <div
                className="bg-muted/40 border-border text-muted-foreground rounded-lg border p-2.5 text-sm"
                data-testid="login-kernel-status"
              >
                <span className="mr-1.5 inline-block h-2 w-2 animate-pulse rounded-full bg-primary align-middle" />
                {kernelStageText(kernelStatus)}…（已等待 {waitingSecs} 秒）
              </div>
            ) : kernelGiveUp ? (
              <div
                className="bg-destructive/10 text-destructive break-all rounded-lg p-2.5 text-sm"
                data-testid="login-kernel-giveup"
              >
                {kernelGiveUp}
              </div>
            ) : (
              error && (
                <div
                  className="bg-destructive/10 text-destructive rounded-lg p-2.5 text-sm"
                  data-testid="login-modal-error"
                >
                  {error}
                </div>
              )
            )}

            {/* 用户名 */}
            <div className="space-y-1.5">
              <label
                htmlFor="login-modal-username"
                className="text-foreground block text-sm font-medium"
              >
                用户名 <span className="text-destructive">*</span>
              </label>
              <Input
                id="login-modal-username"
                type="text"
                value={username}
                onChange={(e) => {
                  manualEditRef.current = true
                  setUsername(e.target.value)
                }}
                placeholder="请输入用户名"
                disabled={isLoading}
                aria-invalid={!!formErrors.username}
                data-testid="login-modal-username"
                className="h-9"
                autoFocus
              />
              {formErrors.username && (
                <p className="text-destructive text-xs">{formErrors.username}</p>
              )}
            </div>

            {/* 密码 */}
            <div className="space-y-1.5">
              <label
                htmlFor="login-modal-password"
                className="text-foreground block text-sm font-medium"
              >
                密码 <span className="text-destructive">*</span>
              </label>
              <Input
                id="login-modal-password"
                type="password"
                value={password}
                onChange={(e) => {
                  manualEditRef.current = true
                  setPassword(e.target.value)
                }}
                placeholder="请输入密码"
                disabled={isLoading}
                aria-invalid={!!formErrors.password}
                data-testid="login-modal-password"
                className="h-9"
              />
              {formErrors.password && (
                <p className="text-destructive text-xs">{formErrors.password}</p>
              )}
            </div>

            <Button
              type="submit"
              className="h-9 w-full"
              disabled={isLoading}
              data-testid="login-modal-submit"
            >
              {isLoading ? '登录中...' : '登录'}
            </Button>

            {/* 当前目标恢复面：自定义目标在连时呈现（死目标登录闸的自助口，
                S5-D② 服务器▾ 可发现性债的偿还面） */}
            {currentTarget && (
              <div
                data-testid="login-current-target"
                className="bg-muted/40 border-border rounded-md border px-3 py-2.5"
              >
                <p className="text-muted-foreground text-xs">
                  当前连接：{' '}
                  <span className="text-foreground font-medium" data-testid="login-current-target-base">
                    {currentTarget}
                  </span>
                </p>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="mt-2 w-full"
                  disabled={switchingLocal || isLoading}
                  onClick={switchToLocalKernel}
                  data-testid="login-use-local-kernel"
                >
                  {switchingLocal ? '正在切回本机内核…' : '改用本机内核'}
                </Button>
                {localSwitchError && (
                  <p className="text-destructive mt-1.5 text-xs" data-testid="login-local-switch-error">
                    {localSwitchError}
                  </p>
                )}
              </div>
            )}

            {/* 注册引导 + 服务器入口（自整页登录版并入，ADR 2026-09-28） */}
            <p className="text-muted-foreground text-center text-sm">
              没有账号？{' '}
              <Link
                to={ROUTES.REGISTER}
                data-testid="login-modal-register-link"
                className="text-primary hover:underline"
              >
                注册
              </Link>
            </p>

            {/* 次要入口：服务器地址（登录前可达，换后端发生在登录前） */}
            <div className="border-t pt-3">
              <button
                type="button"
                onClick={() => setServerOpen((o) => !o)}
                className="text-muted-foreground hover:text-foreground mx-auto flex items-center gap-1 text-xs"
                data-testid="login-server-toggle"
              >
                服务器 {serverOpen ? '▴' : '▾'}
              </button>
              {serverOpen && (
                <div className="mt-3">
                  <ServerAddressForm />
                </div>
              )}
            </div>
          </form>
        </div>
      </DialogContent>
    </Dialog>
  )
}

export default LoginModal
