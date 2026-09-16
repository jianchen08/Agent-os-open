/**
 * 确认弹窗（共享件）
 *
 * useConfirmDialog 的渲染配对件：dialogState.open 时渲染 DOM 可见确认层
 * （role=dialog + 遮罩点击等同取消 + 取消/确认按钮）。不用原生
 * window.confirm——自动化浏览器对原生对话框静默 auto-dismiss，点击流
 * 无声中断（零请求零反馈，BUG-23）。
 *
 * @module ConfirmDialog
 */
import type { ConfirmDialogState } from '@/utils/confirm'

export function ConfirmDialog({ dialogState }: { dialogState: ConfirmDialogState }) {
  if (!dialogState.open) return null
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      role="dialog"
      aria-modal="true"
      aria-label="确认操作"
    >
      <div
        className="fixed inset-0 bg-[var(--overlay-bg)]"
        onClick={() => {
          dialogState.onCancel()
        }}
      />
      <div className="bg-background border-border relative z-10 mx-4 w-full max-w-sm rounded-lg border p-4 shadow-lg">
        <p className="text-foreground mb-4 text-sm">{dialogState.message}</p>
        <div className="flex justify-end gap-2">
          <button
            onClick={() => dialogState.onCancel()}
            className="border-border hover:bg-muted/70 text-muted-foreground rounded-md border px-3 py-1.5 text-xs transition-colors"
          >
            取消
          </button>
          <button
            onClick={() => dialogState.onConfirm()}
            className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-md px-3 py-1.5 text-xs transition-colors"
          >
            确认
          </button>
        </div>
      </div>
    </div>
  )
}
