/**
 * 本机卡（注册表统一渲染：device_id 命中本端的行渲染本卡）：身份 + 连接态 +
 * 内核探活。身份来自 /ext/sync_agent/status（device_id/档位/配对态）；连接态
 * 两档同源 localStorage[server_base_url]（空 = 本机内核使用中；有值 = 自定义
 * 档使用中，可一键切回）。探活 = GET {生效地址}/health（connectionProbe 契约：
 * 2xx 即通，含内核版本回显）。
 */
import { useEffect, useState } from 'react'
import { API_BASE_URL } from '@/constants/api'
import { useSyncAgentStatusQuery } from '@/hooks/queries/useSyncMirrorQuery'
import { connectKernel } from '@/pages/settings/devices/connectKernel'
import { formatStorageLine } from '@/services/api/syncMirror'
import { probeConnection, type ConnectionProbeResult } from '@/services/connectionProbe'
import { getServerBaseUrl, getShellApiBase } from '@/services/shellApiBase'

type ProbeState =
  | { phase: 'idle' }
  | { phase: 'probing' }
  | { phase: 'done'; result: ConnectionProbeResult }

export function SelfDeviceCard({ connected }: { connected: boolean }) {
  const statusQuery = useSyncAgentStatusQuery()
  const [probe, setProbe] = useState<ProbeState>({ phase: 'idle' })
  /** 用户设置值（空串 = 本机内核档；挂载期读存储，切回路径以刷新收尾不回写） */
  const [userBase, setUserBase] = useState<string>('')
  /** 切回本机内核的提示（随后整页刷新）与失败提示 */
  const [savedHint, setSavedHint] = useState<string | null>(null)

  useEffect(() => {
    setUserBase(getServerBaseUrl() ?? '')
  }, [])

  const status = statusQuery.data ?? null
  const effectiveBase = API_BASE_URL
  const effectiveLabel = effectiveBase || '同源（相对路径）'
  const shellDefaultBase = getShellApiBase()
  const localMode = userBase === ''
  /** 探活目标 = 当前生效地址（本机档即本机内核，远端档即所连内核） */
  const probeTarget = effectiveBase

  /** 切回本机内核档：经 connectKernel 单点（清用户设置 + 原生起本地栈 + 刷新；
   * warm 启动 ~13-15s，登录由本机档免填重试窗口兜底，页面刷新后即有加载提示） */
  const switchToLocal = () => {
    if (localMode) return
    void connectKernel('').then((result) => {
      if (result.ok) {
        setSavedHint('已切回本机内核，本机栈启动中（约 15 秒）…页面即将刷新，登录将自动重试连接')
      } else {
        setSavedHint(result.error)
      }
    })
  }

  return (
    <div
      className={`rounded-lg border p-4 ${connected ? 'border-[var(--ds-border-active)]' : 'border-border'}`}
      data-testid="device-self-card"
    >
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs" data-testid="device-self-identity">
          {status === null ? (
            <span className="text-muted-foreground">本机同步状态不可用</span>
          ) : (
            <>
              <span className="font-medium">本机</span>
              <span className="text-muted-foreground">
                {' '}
                · 档位 {status.tier ?? '?'} ·{' '}
                {status.storage && <>{formatStorageLine(status.storage)} · </>}
                {status.paired ? (
                  <>
                    已配对 <span className="font-mono">{status.device_id ?? '?'}</span>
                  </>
                ) : (
                  '未配对（在下方「设备配对与多端同步」区配对后可多端同步）'
                )}
              </span>
            </>
          )}
        </p>
        {connected && (
          <span
            className="text-[var(--ds-accent-primary)] shrink-0 text-xs"
            data-testid="self-connected-badge"
          >
            已连接
          </span>
        )}
      </div>

      {/* ── 连接态（连接动作长在卡里）：本机内核档 / 自定义档使用中 ── */}
      <div
        className={`mt-3 rounded-lg border p-3 ${localMode ? 'border-[var(--ds-border-active)]' : 'border-border'}`}
        data-testid="backend-mode-local"
      >
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <div className="text-sm font-medium">本机内核（默认）</div>
            <div
              className="text-muted-foreground mt-0.5 truncate font-mono text-xs"
              data-testid="backend-local-base"
            >
              {shellDefaultBase || '同源（相对路径）'}
            </div>
          </div>
          {localMode ? (
            <span className="text-[var(--ds-accent-primary)] shrink-0 text-xs">使用中</span>
          ) : (
            <button
              type="button"
              onClick={switchToLocal}
              className="border-border hover:bg-muted/30 shrink-0 rounded-lg border px-3 py-1.5 text-xs"
              data-testid="backend-use-local-button"
            >
              切换
            </button>
          )}
        </div>
        <p className="text-muted-foreground mt-2 text-xs">
          {localMode
            ? '连接本机（或壳内置）内核；登录凭据自动生成，无需填写。'
            : getShellApiBase() !== null
              ? '本机内核已停止（运行消耗回落纯壳档）；切回后原生侧重启本机栈。'
              : '本机内核仍在后台运行；当前会话连接的是下方地址，两者互不影响。切换后本页刷新回到本机内核。'}
        </p>
      </div>
      {savedHint && (
        <p className="text-status-info mt-3 text-xs" data-testid="connection-saved-hint">
          {savedHint}
        </p>
      )}

      {/* ── 内核探活（当前生效地址；编辑中的地址优先）──────────────── */}
      <div className="mt-3 flex items-center gap-2">
        <span
          className={`inline-block h-2 w-2 rounded-full ${
            probe.phase === 'done'
              ? probe.result.ok
                ? 'bg-status-success'
                : 'bg-destructive'
              : 'bg-muted-foreground/40'
          }`}
          data-testid="connection-status-dot"
        />
        {/* 生效地址是 URL 长单 token：flex 内联子项须 min-w-0 放开收缩 + break-all 断行（地址无散文，不伤正常排版） */}
        <span className="min-w-0 break-all font-mono text-sm" data-testid="connection-effective-base">
          {effectiveLabel}
        </span>
      </div>
      <div className="mt-3 flex items-center gap-3">
        <button
          type="button"
          onClick={() => {
            setProbe({ phase: 'probing' })
            void probeConnection(probeTarget).then((result) => setProbe({ phase: 'done', result }))
          }}
          disabled={probe.phase === 'probing'}
          className="border-border hover:bg-muted/30 rounded-lg border px-3 py-1.5 text-xs"
          data-testid="connection-test-button"
        >
          {probe.phase === 'probing' ? '测试中…' : '测试连接'}
        </button>
        {probe.phase === 'done' && (
          <span className="text-muted-foreground text-xs" data-testid="connection-probe-result">
            {probe.result.ok
              ? `在线 · ${probe.result.latencyMs}ms` +
                (probe.result.kernelVersion ? ` · 内核 ${probe.result.kernelVersion}` : '')
              : `离线（${probe.result.status === 0 ? '网络不可达' : `HTTP ${probe.result.status}`}）`}
          </span>
        )}
      </div>
      <p className="text-muted-foreground mt-3 text-xs" data-testid="device-self-note">
        本机即当前设备：本机会话请直接在左侧会话列表使用。
      </p>
    </div>
  )
}
