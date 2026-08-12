/**
 * AgentConfigModal 组件
 *
 * Agent 列表页「编辑」入口：Radix Dialog 包裹 SchemaFormEmbed，
 * 实现「加载 schema 字段 + yaml → SchemaDriver 表单 → 保存 PUT 写回」闭环。
 *
 * 结构对齐 SessionEditModal（Dialog/DialogContent/DialogHeader/DialogFooter + Button）。
 */

import { SchemaFormEmbed } from '@/components/schema/widgets/SchemaFormEmbed'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'

/** AgentConfigModal 属性 */
export interface AgentConfigModalProps {
  /** 当前编辑的 Agent（null 时不渲染内容） */
  agent: { id: string; name?: string } | null
  /** 是否打开 */
  isOpen: boolean
  /** 关闭回调 */
  onClose: () => void
  /** 保存成功回调（列表页刷新） */
  onSaved?: () => void
}

/**
 * Agent 配置编辑模态框
 *
 * @param props - agent/isOpen/onClose/onSaved
 * @returns Radix Dialog 模态框
 */
export function AgentConfigModal({ agent, isOpen, onClose, onSaved }: AgentConfigModalProps) {
  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        className="w-full"
        style={{ width: 'min(920px, 95vw)', maxHeight: '90vh' }}
      >
        <DialogHeader>
          <DialogTitle>编辑配置 — {agent?.name ?? agent?.id ?? ''}</DialogTitle>
          <DialogDescription>
            基于字段 Schema 编辑 {agent?.id} 的配置，保存时后端自动备份原文件
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto p-6 pt-4">
          {agent ? (
            <SchemaFormEmbed
              key={agent.id}
              schemaId={agent.id}
              onSaved={() => {
                onSaved?.()
                onClose()
              }}
            />
          ) : (
            <div className="text-muted-foreground p-4 text-sm">请选择要编辑的 Agent</div>
          )}
        </div>

        <DialogFooter>
          <Button variant="outline" size="sm" onClick={onClose}>
            取消
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default AgentConfigModal
