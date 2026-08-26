/**
 * PendingInputQueueBar——管道待处理输入队列条（ADR-2026-08-26）
 *
 * 位置：聊天输入框上方。管道执行中发送的消息在此排队（等待窗口内可编辑/
 * 删除/清空）；消费激活后进主消息流（stream_start 接管）。空队列零渲染。
 *
 * 交互：
 * - 点击条目 → 回填输入框（编辑态，Enter 保存覆盖、Esc 取消）
 * - 条目 × → 删除单条；"清空队列" → 全部删除
 */
import { Bell, Check, Clock, Send, Trash2, X } from 'lucide-react'
import { useState } from 'react'
import { Button } from '@/components/ui/button'
import { usePendingInputStore } from '@/stores/pendingInputStore'
import type { PendingInputItem } from '@/services/api/pipelines'

/** 空队列快照必须是模块级常量：zustand v5 裸 useSyncExternalStore 只做 Object.is
 * 比较，selector 内联 `?? []` 每次产生新数组引用 → forceStoreRerender 无限循环
 * （同 ChatContainer EMPTY_MESSAGES 范式）。 */
const EMPTY_ITEMS: PendingInputItem[] = []

const SOURCE_LABELS: Record<string, string> = {
  user: '人',
  trigger: '触发器',
  task: '任务',
  http: 'HTTP',
  system: '系统',
}

export interface PendingInputQueueBarProps {
  /** 当前标签管道 ID（主标签=主管道，子标签=子管道） */
  pipelineId: string
}

export function PendingInputQueueBar({ pipelineId }: PendingInputQueueBarProps) {
  const items = usePendingInputStore((s) => s.byPipeline[pipelineId] ?? EMPTY_ITEMS)
  const editingId = usePendingInputStore((s) => s.editingId[pipelineId] ?? null)
  const remove = usePendingInputStore((s) => s.remove)
  const clear = usePendingInputStore((s) => s.clear)
  const updateContent = usePendingInputStore((s) => s.updateContent)
  const setEditing = usePendingInputStore((s) => s.setEditing)
  const [expanded, setExpanded] = useState(false)
  const [editDraft, setEditDraft] = useState('')

  if (items.length === 0) return null

  const startEdit = (inputId: string, content: string) => {
    setEditing(pipelineId, inputId)
    setEditDraft(content)
    setExpanded(true)
  }

  const saveEdit = (inputId: string) => {
    const trimmed = editDraft.trim()
    if (!trimmed) return
    void updateContent(pipelineId, inputId, trimmed)
    setEditing(pipelineId, null)
  }

  const cancelEdit = () => {
    setEditing(pipelineId, null)
  }

  return (
    <div
      data-testid="pending-queue-bar"
      className="border-border/60 bg-muted/40 mb-2 flex flex-col gap-1 rounded-lg border px-2 py-1.5 text-xs"
    >
      {/* 收起态：N 条待处理 + 首条预览 */}
      <div className="flex items-center gap-2">
        <Clock className="text-muted-foreground h-icon-sm w-icon-sm shrink-0" />
        <button
          type="button"
          className="text-foreground flex min-w-0 flex-1 items-center gap-1.5 text-left"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          <span className="text-muted-foreground shrink-0">
            {items.length} 条待处理
          </span>
          <span className="text-muted-foreground/70 min-w-0 flex-1 truncate">
            {items[0].content}
          </span>
          {items.length > 1 && (
            <span className="text-muted-foreground/50 shrink-0">+{items.length - 1}</span>
          )}
        </button>
        <Button
          variant="ghost"
          size="sm"
          className="text-muted-foreground hover:text-destructive h-6 px-1.5 text-xs"
          onClick={() => void clear(pipelineId)}
          title="清空队列"
          aria-label="清空队列"
        >
          <Trash2 className="h-icon-xs w-icon-xs" />
        </Button>
      </div>

      {/* 展开态：逐条列表（预览/来源/时间/编辑/删除） */}
      {expanded && (
        <ul className="flex flex-col gap-1" data-testid="pending-queue-list">
          {items.map((item, idx) => {
            const isEditing = editingId === item.id
            return (
              <li
                key={item.id}
                className="border-border/50 flex items-center gap-2 rounded border px-1.5 py-1"
              >
                <span className="text-muted-foreground/60 w-4 shrink-0 text-right">{idx + 1}</span>
                <span className="text-muted-foreground/80 shrink-0 rounded bg-[var(--hover-overlay)] px-1 py-0.5 text-[10px]">
                  {SOURCE_LABELS[item.source] ?? item.source}
                </span>
                {isEditing ? (
                  <>
                    <input
                      autoFocus
                      value={editDraft}
                      onChange={(e) => setEditDraft(e.target.value)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') saveEdit(item.id)
                        if (e.key === 'Escape') cancelEdit()
                      }}
                      className="bg-transparent min-w-0 flex-1 outline-none"
                      aria-label={`编辑第 ${idx + 1} 条`}
                    />
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-5 w-5 p-0"
                      onClick={() => saveEdit(item.id)}
                      aria-label="保存修改"
                    >
                      <Check className="h-icon-xs w-icon-xs" />
                    </Button>
                  </>
                ) : (
                  <>
                    <button
                      type="button"
                      className="text-foreground min-w-0 flex-1 truncate text-left hover:underline"
                      onClick={() => startEdit(item.id, item.content)}
                      title="点击修改"
                    >
                      {item.content}
                    </button>
                    <span className="text-muted-foreground/50 shrink-0 text-[10px]">
                      {new Date(item.created_at).toLocaleTimeString()}
                    </span>
                    <Button
                      variant="ghost"
                      size="sm"
                      className="text-muted-foreground hover:text-destructive h-5 w-5 p-0"
                      onClick={() => void remove(pipelineId, item.id)}
                      aria-label="删除本条"
                    >
                      <X className="h-icon-xs w-icon-xs" />
                    </Button>
                  </>
                )}
              </li>
            )
          })}
        </ul>
      )}

      {/* 提示：队列消息将在当前回复结束后按序自动发出 */}
      <div className="text-muted-foreground/60 flex items-center gap-1 text-[10px]">
        <Send className="h-icon-xs w-icon-xs" />
        <span>当前回复结束后自动按序发送；点击条目可修改</span>
        {items.some((i) => i.source !== 'user') && (
          <span className="flex items-center gap-0.5">
            <Bell className="h-icon-xs w-icon-xs" />
            <span>含触发器/任务注入</span>
          </span>
        )}
      </div>
    </div>
  )
}
