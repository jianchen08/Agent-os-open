/**
 * SkinConsentDialog — 皮肤 hooks 启用确认卡（2026-09-25 准入分级波2）
 *
 * skinRuntime 对 hooks.mjs 文本算 sha256 后判定首启用/漂移 → 不执行，载荷挂
 * skinConsentStore；本组件（App 根全局浮层）消费 store 呈确认卡：
 * - 确认 → pin 写入 localStorage + 清 pending + 重应用皮肤（hooks 随之运行）；
 * - 取消 → 清 pending（静态 CSS 已生效，动态层不跑）。
 *
 * 语义：插件 JS 在宿主执行是最危险的插件能力，用户须有可指认的同意时刻；
 * 指纹漂移（皮肤作者更新脚本/文件被篡改）后旧 pin 不再放行，须重新确认。
 */

import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { applyPluginSkin, saveSkinHookPin } from '@/services/skinRuntime'
import { useSkinConsentStore } from '@/stores/skinConsentStore'

/** 指纹展示压缩：全 hex 过长，取前后段足够指认（完整性由比对逻辑保证） */
function shortHash(hash: string): string {
  return hash.length > 16 ? `${hash.slice(0, 8)}…${hash.slice(-8)}` : hash
}

export function SkinConsentDialog(): React.ReactNode {
  const pending = useSkinConsentStore((s) => s.pending)
  const setPending = useSkinConsentStore((s) => s.setPending)
  const [busy, setBusy] = useState(false)

  if (!pending) return null

  const confirm = async (): Promise<void> => {
    setBusy(true)
    try {
      saveSkinHookPin(pending.scope, pending.hash)
      setPending(null)
      // 重应用皮肤：pin 已写入，hooks 本次直接运行（静态层幂等重建）
      await applyPluginSkin(pending.theme)
    } finally {
      setBusy(false)
    }
  }

  const cancel = (): void => {
    setPending(null)
  }

  return (
    <div
      className="fixed inset-0 z-[1100] flex items-center justify-center bg-black/50"
      data-testid="skin-consent-overlay"
    >
      <div className="bg-background border-border mx-4 w-full max-w-md rounded-lg border p-5 shadow-xl">
        <h2 className="text-foreground text-base font-semibold">
          {pending.reason === 'drift' ? '皮肤脚本已变更，需重新确认' : '启用皮肤脚本'}
        </h2>
        <div className="text-muted-foreground mt-3 space-y-2 text-sm">
          <p>
            皮肤「{pending.theme.name ?? pending.theme.skin}」（来源插件{' '}
            <span className="text-foreground font-mono">{pending.theme.pluginId}</span>
            ）包含将在应用内执行的脚本（装饰动画/立绘/标题栏等动态效果）。
          </p>
          {pending.reason === 'drift' && (
            <p className="text-status-warning">
              脚本指纹与上次确认不一致：{shortHash(pending.previous ?? '')} →{' '}
              {shortHash(pending.hash)}。若非你预期的皮肤更新，请拒绝。
            </p>
          )}
          <p>
            本次脚本指纹：<span className="text-foreground font-mono">{shortHash(pending.hash)}</span>
          </p>
          <p className="text-xs">拒绝后皮肤静态样式仍生效，仅动态装饰效果不可用。</p>
        </div>
        <div className="mt-4 flex justify-end gap-2">
          <Button variant="outline" size="sm" onClick={cancel} disabled={busy}>
            仅用静态样式
          </Button>
          <Button size="sm" onClick={() => void confirm()} disabled={busy} data-testid="skin-consent-confirm">
            确认执行
          </Button>
        </div>
      </div>
    </div>
  )
}
