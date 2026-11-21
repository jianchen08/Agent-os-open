/**
 * TabBulkCloseDialog —— 批量关签确认层（对话标签与工作区标签共用）
 *
 * 契约（BUG-79 实证入口防误触，两栏一致）：批量关签不可逆，先出确认层并
 * 如实标注影响数量；`keepNote` 说明哪些签不受影响（工作区=固定签；对话流=
 * 固定签 + 主对话签）。影响数为 0 时调用方直接执行、不出确认层。
 */

import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import type { TabBulkCloseAction } from '@/utils/tabCloseSemantics'

export interface TabBulkCloseRequest {
  action: TabBulkCloseAction
  /** 关签基准签（closeOther 保留目标签；closeAll 不使用） */
  tabId: string
  /** 将被关闭的标签数 */
  count: number
}

export interface TabBulkCloseDialogProps {
  request: TabBulkCloseRequest | null
  /** 不受批量关签影响的保留说明（默认「固定标签将保留」） */
  keepNote?: string
  onConfirm: () => void
  onCancel: () => void
  /** 确认按钮测试定位前缀（`workspace-tab-bulk-close` / `agent-tab-bulk-close`） */
  testIdPrefix?: string
}

export function TabBulkCloseDialog({
  request,
  keepNote = '固定标签将保留',
  onConfirm,
  onCancel,
  testIdPrefix,
}: TabBulkCloseDialogProps) {
  const isCloseOther = request?.action === 'closeOther'
  return (
    <Dialog open={!!request} onOpenChange={(open) => !open && onCancel()}>
      <DialogContent className="max-w-[380px]">
        <DialogHeader>
          <DialogTitle>{isCloseOther ? '关闭其他标签' : '关闭所有标签'}</DialogTitle>
          <DialogDescription>
            {isCloseOther
              ? `将关闭其余 ${request?.count ?? 0} 个标签，${keepNote}。此操作不可批量恢复，确定继续吗？`
              : `将关闭全部 ${request?.count ?? 0} 个标签，${keepNote}。此操作不可批量恢复，确定继续吗？`}
          </DialogDescription>
        </DialogHeader>
        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onCancel}>
            取消
          </Button>
          <Button
            variant="destructive"
            size="sm"
            onClick={onConfirm}
            data-testid={testIdPrefix ? `${testIdPrefix}-confirm` : undefined}
          >
            确认关闭
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
