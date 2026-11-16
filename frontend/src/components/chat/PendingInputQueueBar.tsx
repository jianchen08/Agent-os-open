/**
 * PendingInputQueueBar——管道待处理输入队列条（ADR-2026-08-26）
 *
 * 位置：聊天输入框上方。管道执行中发送的消息在此排队；消费激活后进主消息流
 * （stream_start 接管）。空队列零渲染。
 *
 * 交互（2026-10-24 用户裁定）：
 * - 不做折叠：条目恒常平铺、全文显示不省略——排队中的消息用户必须一眼看全
 * - 每条三个显式按钮，操作不必先展开再寻找：
 *   ↩ 回到输入框（出队回填，改完直接重发）/ ⚡ 立即发送（停止当前回复，
 *   本条优先发出）/ × 删除本条；头部「清空队列」全部删除
 * - 排队条目是排队期唯一视图：气泡在引擎消费该条（consumed 事件携带权威
 *   record 走认领链补插）时才出现在消息流，队列条不再承担气泡同步职责
 */
import { Bell, Clock, Trash2, Undo2, X, Zap } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { usePendingInputResync } from '@/hooks/usePendingInputResync'
import { useChatInputStore } from '@/stores/chatInputStore'
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
  /** 立即发送回调（停止当前回复并优先发送本条；编排由上层注入——
   *  组合 DELETE 摘出 + 停止 + 正常发送，需要会话级发送上下文） */
  onForceSend?: (inputId: string) => void
}

export function PendingInputQueueBar({ pipelineId, onForceSend }: PendingInputQueueBarProps) {
  const items = usePendingInputStore((s) => s.byPipeline[pipelineId] ?? EMPTY_ITEMS)
  const remove = usePendingInputStore((s) => s.remove)
  const clear = usePendingInputStore((s) => s.clear)
  // F10 对账：断线期间 consumed/deleted 事件丢失会让条目永久挂显示——挂载 /
  // WS 重连 / 有界轮询三通道 GET 全量覆盖本地镜像（已消费条目后端已删，天然收敛）。
  // 空队列也启用：漏接 enqueued 时本地永远为空，条件化对账等于没有自愈通道。
  usePendingInputResync(pipelineId)

  if (items.length === 0) return null

  /** DELETE 失败不本地出队（条目仍在内核队列，挂显示由 F10 对账收敛）。
   *  [OBS-R258-1 登记] 此处故意不上报：失败语义=条目原样保留，出队真相
   *  以后端权威列表为准，错误弹窗只会重复打扰（对账通道已兜显示一致性）。 */
  const dropEntry = (action: Promise<void>) => {
    action.catch(() => {
      // 删除失败：条目留在队列（DELETE 未生效，本地镜像不许领先内核）
    })
  }

  // 修改=退回输入框：该条出队 + 内容回填输入框（追加语义，与停止退回同通道）
  const returnToInput = (item: PendingInputItem) => {
    useChatInputStore.getState().requestInsert(item.content)
    dropEntry(remove(pipelineId, item.id))
  }

  const removeOne = (item: PendingInputItem) => {
    dropEntry(remove(pipelineId, item.id))
  }

  const clearAll = () => {
    dropEntry(clear(pipelineId))
  }

  return (
    <div
      data-testid="pending-queue-bar"
      className="border-border/60 bg-muted/40 mb-2 flex flex-col gap-1 rounded-lg border px-2 py-1.5 text-xs"
    >
      {/* 头部：条数 + 自动发送说明 + 清空队列 */}
      <div className="flex items-center gap-2">
        <Clock className="text-muted-foreground h-icon-sm w-icon-sm shrink-0" />
        <span className="text-muted-foreground shrink-0">{items.length} 条待处理</span>
        <span className="text-muted-foreground/60 min-w-0 flex-1 truncate">
          当前回复结束后自动按序发送
        </span>
        <Button
          variant="ghost"
          size="sm"
          className="text-muted-foreground hover:text-destructive h-6 px-1.5 text-xs"
          onClick={clearAll}
          title="清空队列"
          aria-label="清空队列"
        >
          <Trash2 className="h-icon-xs w-icon-xs" />
        </Button>
      </div>

      {/* 条目恒常平铺：全文显示（不省略）+ 显式操作按钮组 */}
      <ul
        className="flex max-h-40 flex-col gap-1 overflow-y-auto"
        data-testid="pending-queue-list"
      >
        {items.map((item, idx) => (
          <li
            key={item.id}
            className="border-border/50 flex items-start gap-2 rounded border px-1.5 py-1"
          >
            <span className="text-muted-foreground/60 w-4 shrink-0 text-right">{idx + 1}</span>
            <span className="text-muted-foreground/80 shrink-0 rounded bg-[var(--hover-overlay)] px-1 py-0.5 text-[10px]">
              {SOURCE_LABELS[item.source] ?? item.source}
            </span>
            <div className="text-foreground min-w-0 flex-1 whitespace-pre-wrap break-words">
              {item.content}
            </div>
            <span className="text-muted-foreground/50 shrink-0 text-[10px]">
              {new Date(item.created_at).toLocaleTimeString()}
            </span>
            <div className="flex shrink-0 items-center gap-0.5">
              <Button
                variant="ghost"
                size="sm"
                className="text-muted-foreground hover:text-foreground h-5 w-5 p-0"
                onClick={() => returnToInput(item)}
                title="回到输入框修改（改完直接重新发送）"
                aria-label="回到输入框修改"
              >
                <Undo2 className="h-icon-xs w-icon-xs" />
              </Button>
              {onForceSend && (
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-5 w-5 p-0"
                  onClick={() => onForceSend(item.id)}
                  title="立即发送（停止当前回复并优先发送本条）"
                  aria-label={`立即发送第 ${idx + 1} 条`}
                >
                  <Zap className="h-icon-xs w-icon-xs" />
                </Button>
              )}
              <Button
                variant="ghost"
                size="sm"
                className="text-muted-foreground hover:text-destructive h-5 w-5 p-0"
                onClick={() => removeOne(item)}
                title="删除本条"
                aria-label="删除本条"
              >
                <X className="h-icon-xs w-icon-xs" />
              </Button>
            </div>
          </li>
        ))}
      </ul>

      {/* 非用户来源条目提示（触发器/任务注入也会排队） */}
      {items.some((i) => i.source !== 'user') && (
        <div className="text-muted-foreground/60 flex items-center gap-1 text-[10px]">
          <Bell className="h-icon-xs w-icon-xs" />
          <span>含触发器/任务注入</span>
        </div>
      )}
    </div>
  )
}
