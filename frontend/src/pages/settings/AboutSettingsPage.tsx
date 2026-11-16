/**
 * 关于页（设置中枢子页）
 *
 * 版本信息四项：app 版本（壳注入 versionName/versionCode，源 build.gradle）、
 * 前端构建标识（vite define 注入 git 短哈希 + 构建时间）、
 * 内核版本（GET {base}/health 的 version；云端网关未反代 /health 时降级为
 * 「未透出」）、当前连接服务器地址。
 *
 * 诊断信息块（只读快照，进页采集一次）：安全区链路（壳注入 --safe-inset-* /
 * env() 实测 / 生效避让值 --safe-area-*）+ 视口（innerWidth/Height、DPR）+
 * userAgent + 主题 class——真机（挖孔/edge-to-edge）特有布局问题的远程归因
 * 数据源，用户截图本块即可定位白条/挤开/全屏三类问题。
 */

import { useEffect, useState } from 'react'
import { PageShell } from '@/components/shared/PageShell'
import { API_BASE_URL } from '@/constants/api'
import { probeConnection } from '@/services/connectionProbe'
import { getShellAppVersion } from '@/services/shellApiBase'

/** 键值行 */
function InfoRow({ label, value, testid }: { label: string; value: string; testid?: string }) {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-border/60 py-2.5 last:border-b-0">
      {/* 标签防挤压断词（窄栏下“前端构建”曾被折成“前端构/建”），长值右对齐换行 */}
      <span className="text-muted-foreground shrink-0 whitespace-nowrap text-sm">{label}</span>
      <span className="font-mono text-sm min-w-0 break-all text-right" data-testid={testid}>
        {value}
      </span>
    </div>
  )
}

/** 诊断快照：安全区链路 + 视口 + UA + 主题 class */
interface DiagSnapshot {
  shellInsetTop: string
  shellInsetBottom: string
  safeAreaTop: string
  safeAreaBottom: string
  envTop: string
  envBottom: string
  envLeft: string
  envRight: string
  viewport: string
  dpr: string
  userAgent: string
  themeClass: string
}

/** env() 无法经 getComputedStyle 直读：探针元素逐向挂 padding 实测
 * （env 不支持/为 0 时回落 0px，声明始终合法）。 */
function measureEnvInsets(): { top: string; bottom: string; left: string; right: string } {
  const probe = document.createElement('div')
  probe.style.position = 'absolute'
  probe.style.visibility = 'hidden'
  probe.style.paddingTop = 'env(safe-area-inset-top, 0px)'
  probe.style.paddingBottom = 'env(safe-area-inset-bottom, 0px)'
  probe.style.paddingLeft = 'env(safe-area-inset-left, 0px)'
  probe.style.paddingRight = 'env(safe-area-inset-right, 0px)'
  document.body.appendChild(probe)
  const pcs = getComputedStyle(probe)
  const result = {
    top: pcs.paddingTop,
    bottom: pcs.paddingBottom,
    left: pcs.paddingLeft,
    right: pcs.paddingRight,
  }
  probe.remove()
  return result
}

/** 采集当前诊断快照（进页一次；壳变量经内联 style 注入在 documentElement 上，
 * 壳未写时值为空串——空串本身即「壳脚本未跑」的诊断信号，如实透出）。 */
function readDiagSnapshot(): DiagSnapshot {
  const cs = getComputedStyle(document.documentElement)
  const env = measureEnvInsets()
  return {
    shellInsetTop: cs.getPropertyValue('--safe-inset-top').trim(),
    shellInsetBottom: cs.getPropertyValue('--safe-inset-bottom').trim(),
    safeAreaTop: cs.getPropertyValue('--safe-area-top').trim(),
    safeAreaBottom: cs.getPropertyValue('--safe-area-bottom').trim(),
    envTop: env.top,
    envBottom: env.bottom,
    envLeft: env.left,
    envRight: env.right,
    viewport: `${window.innerWidth} × ${window.innerHeight}`,
    dpr: String(window.devicePixelRatio),
    userAgent: navigator.userAgent,
    themeClass: document.documentElement.classList.contains('dark') ? 'dark（有）' : '无（浅色）',
  }
}

export function AboutSettingsPage({ embedded = false }: { embedded?: boolean }) {
  const [kernelVersion, setKernelVersion] = useState<string | null>(null)
  const [diag] = useState<DiagSnapshot | null>(() =>
    typeof document === 'undefined' ? null : readDiagSnapshot(),
  )

  useEffect(() => {
    void probeConnection(API_BASE_URL).then((r) => setKernelVersion(r.kernelVersion))
  }, [])

  const appVersion = getShellAppVersion()
  const appVersionLabel = appVersion
    ? `${appVersion.versionName ?? '?'} (${appVersion.versionCode ?? '?'})`
    : '—（Web/桌面端无应用版本）'
  const kernelLabel = kernelVersion ?? '—（当前网关未透出内核版本）'
  const serverLabel = API_BASE_URL || '同源（相对路径）'

  const content = (
    <>
      <section className="mb-8">
        <h2 className="mb-3 text-sm font-semibold">版本信息</h2>
        <div className="border-border rounded-lg border px-4 py-1">
          <InfoRow label="应用版本" value={appVersionLabel} testid="about-app-version" />
          <InfoRow
            label="前端构建"
            value={`${__LINGXI_BUILD__.git} · ${__LINGXI_BUILD__.time}`}
            testid="about-frontend-build"
          />
          <InfoRow label="内核版本" value={kernelLabel} testid="about-kernel-version" />
          <InfoRow label="连接服务器" value={serverLabel} testid="about-server-base" />
        </div>
      </section>
      {diag && (
        <section className="mb-8">
          <h2 className="mb-3 text-sm font-semibold">诊断信息</h2>
          <p className="text-muted-foreground mb-2 text-xs">
            只读快照（进页采集）。真机布局问题（白条/挤开/全屏）远程归因：请整块截图发回。
          </p>
          <div className="border-border rounded-lg border px-4 py-1">
            <InfoRow
              label="壳注入 --safe-inset-top"
              value={diag.shellInsetTop === '' ? '（壳未写入）' : diag.shellInsetTop}
              testid="diag-shell-inset-top"
            />
            <InfoRow
              label="壳注入 --safe-inset-bottom"
              value={diag.shellInsetBottom === '' ? '（壳未写入）' : diag.shellInsetBottom}
              testid="diag-shell-inset-bottom"
            />
            <InfoRow label="生效避让 --safe-area-top" value={diag.safeAreaTop || '—'} testid="diag-safe-area-top" />
            <InfoRow
              label="生效避让 --safe-area-bottom"
              value={diag.safeAreaBottom || '—'}
              testid="diag-safe-area-bottom"
            />
            <InfoRow label="env safe-area-top" value={diag.envTop || '—'} testid="diag-env-inset-top" />
            <InfoRow label="env safe-area-bottom" value={diag.envBottom || '—'} testid="diag-env-inset-bottom" />
            <InfoRow label="env safe-area-left" value={diag.envLeft || '—'} testid="diag-env-inset-left" />
            <InfoRow label="env safe-area-right" value={diag.envRight || '—'} testid="diag-env-inset-right" />
            <InfoRow label="视口" value={diag.viewport} testid="diag-viewport" />
            <InfoRow label="devicePixelRatio" value={diag.dpr} testid="diag-dpr" />
            <InfoRow label="userAgent" value={diag.userAgent} testid="diag-ua" />
            <InfoRow label="主题 class（html.dark）" value={diag.themeClass} testid="diag-theme" />
          </div>
        </section>
      )}
    </>
  )

  if (embedded) {
    return (
      <PageShell title="关于" embedded>
        {content}
      </PageShell>
    )
  }
  return <PageShell title="关于">{content}</PageShell>
}
