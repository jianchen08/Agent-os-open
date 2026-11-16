import { useCallback } from 'react'
import { Loader2 } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useTaskActing } from '@/hooks/useTaskActing'
import { confirmPauseTask } from '@/services/taskRowActions'

/** 暂停确认目标（行条目的任务侧标识；null = 弹窗关闭） */
export interface PauseConfirmTarget {
  taskId: string
  name: string
}

/** 暂停确认弹窗（原「取消」操作改标「暂停」：接口不变 POST cancel，终态写
 *  stopped 由后端落账——ADR 2026-10-30-task-status-authority-vocabulary-
 *  convergence 决策 7；确认措辞说明可恢复）。confirming 态消费 store 级
 *  actingTaskIds（#11 统一暴露，经 useTaskActing hook 转介——widgets 禁
 *  import @/stores）；失败诚实反馈：弹窗留驻 + toast（不假装成功），成功才关。 */
export function PipelineManagerPauseConfirmDialog({
  target,
  onClose,
}: {
  target: PauseConfirmTarget | null
  onClose: () => void
}) {
  /** 确认进行中 = 该任务在行级操作集合（与行按钮 pending 同一真值源） */
  const confirming = useTaskActing(target?.taskId)

  /** 确认暂停（编排与失败反馈收敛在 services/taskRowActions.confirmPauseTask）。
   *  失败：toast 落在操作点 + 弹窗留驻（可就地重试），不关弹窗装成功。 */
  const handleConfirm = useCallback(async () => {
    if (!target || confirming) return
    if (await confirmPauseTask(target.taskId)) {
      onClose()
    }
  }, [target, confirming, onClose])

  return (
    <Dialog
      open={!!target}
      onOpenChange={(open) => {
        if (!open && !confirming) onClose()
      }}
    >
      <DialogContent className="max-w-[420px]">
        <DialogHeader>
          <DialogTitle>暂停任务</DialogTitle>
          <DialogDescription>
            确定要暂停任务「{target?.name}」吗？任务将暂停，可恢复。
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose} disabled={confirming}>
            取消
          </Button>
          <Button size="sm" onClick={handleConfirm} disabled={confirming}>
            {confirming ? (
              <>
                <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                暂停中...
              </>
            ) : (
              '确认暂停'
            )}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
