/**
 * 会话列表组件
 *
 * 在侧边栏中渲染会话列表，每项支持：
 * - 点击切换会话
 * - Hover 或活跃会话时显示三点菜单（MoreHorizontal 图标）
 * - 三点菜单支持：编辑、复制、星标、置顶、删除操作
 * - 删除前弹出确认对话框（使用 shadcn/ui Dialog）
 *
 * 使用 memo 优化渲染性能，避免不必要的重渲染。
 */

import {
  Copy,
  Edit3,
  Loader2,
  MoreHorizontal,
  Pin,
  RefreshCw,
  Star,
  Trash2,
} from '@/assets/icons'
import { memo, useCallback, useMemo, useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { cn } from '@/lib/utils'
import type { Session } from '@/types/models'

interface SessionListProps {
  /** 会话列表数据 */
  sessions: Session[]
  /** 当前活跃会话 ID */
  activeSessionId: string | null
  /** 正在删除中的会话 ID 集合 */
  deletingSessionIds: Set<string>
  /** 点击会话回调 */
  onSessionClick: (sessionId: string) => void
  /** 删除会话回调 */
  onDeleteSession: (sessionId: string) => Promise<void>
  /** 编辑会话回调 */
  onEditSession: (session: Session) => void
  /** 复制会话回调 */
  onCopySession: (session: Session) => void
  /** 星标切换回调 */
  onStarSession: (sessionId: string) => void
  /** 置顶切换回调 */
  onPinSession: (sessionId: string) => void
  /** 重置消息回调（P3：重置消息 → 刷新整个前端页面） */
  onResetMessages?: (sessionId: string) => void
  /** 自定义容器类名 */
  className?: string
  /** 列表项高度 */
  itemHeight?: number
}

/**
 * 单个会话列表项组件
 * 使用 memo 包裹以避免列表中某项变化导致整个列表重渲染
 */
interface SessionItemProps {
  /** 会话数据 */
  session: Session
  /** 是否为当前活跃会话 */
  isActive: boolean
  /** 是否正在删除中 */
  isDeleting: boolean
  /** 点击回调 */
  onClick: () => void
  /** 删除按钮点击回调（打开确认对话框） */
  onDelete: () => void
  /** 编辑回调 */
  onEdit: () => void
  /** 复制回调 */
  onCopy: () => void
  /** 星标切换回调 */
  onStar: () => void
  /** 置顶切换回调 */
  onPin: () => void
  /** 重置消息回调（P3） */
  onResetMessages?: () => void
  /** 列表项高度 */
  itemHeight: number
}

const SessionItem = memo<SessionItemProps>(
  ({
    session,
    isActive,
    isDeleting,
    onClick,
    onDelete,
    onEdit,
    onCopy,
    onStar,
    onPin,
    onResetMessages,
    itemHeight,
  }) => {
    return (
      <div
        className={cn(
          'group relative flex flex-col justify-center rounded-lg px-2.5 transition-colors',
          isActive
            ? 'bg-[var(--ds-bg-elevated,#111C38)] text-foreground ring-1 ring-[var(--ds-border-active,rgba(34,211,238,0.45))]'
            : 'hover:bg-[var(--ds-bg-hover,#1A2748)] cursor-pointer',
          isDeleting && 'pointer-events-none opacity-50',
        )}
        style={{ height: `${itemHeight}px` }}
        onClick={onClick}
        role="button"
        tabIndex={0}
        aria-label={`会话: ${session.title}`}
        aria-current={isActive ? 'true' : undefined}
      >
        {/* 标题行 */}
        <div className="flex items-center gap-1">
          <span
            className={cn(
              'min-w-0 flex-1 truncate text-[13px]',
              isActive ? 'font-medium text-foreground' : 'text-[var(--ds-text-secondary,#CBD5E1)]',
            )}
          >
            {session.pinned && (
              <Pin
                className="mr-1 inline h-3 w-3 fill-[var(--ds-accent-primary,#22D3EE)] text-[var(--ds-accent-primary,#22D3EE)]"
                data-testid="pin-icon"
              />
            )}
            {session.title}
          </span>

          {/* 工作空间徽标 */}
          {session.workspace && (
            <span
              title={`工作空间: ${session.workspace}`}
              className="text-muted-foreground ml-1 hidden max-w-[120px] flex-shrink-0 truncate rounded bg-[var(--hover-overlay)] px-1.5 py-0.5 text-[10px] sm:block"
            >
              📁 {session.isolationMode === 'isolated' ? '🛡️' : ''}
              {session.workspace.split(/[\\/]/).filter(Boolean).pop() || session.workspace}
            </span>
          )}

          {/* 星标简化：单一点击切换收藏（金色实心=已收藏，灰色描边=未收藏） */}
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation()
              onStar()
            }}
            className="shrink-0 rounded p-0.5 transition-opacity hover:opacity-80"
            aria-label={session.starred ? '取消星标' : '星标'}
            title={session.starred ? '取消星标' : '星标'}
            data-testid="star-button"
          >
            <Star
              className={cn(
                'h-4 w-4 transition-all duration-200',
                session.starred
                  ? 'fill-amber-400 text-status-warning drop-shadow-[0_0_3px_rgba(251,191,36,0.6)]'
                  : 'fill-none stroke-current stroke-[1.5] text-muted-foreground opacity-60 group-hover:text-status-warning group-hover:opacity-100',
              )}
              data-testid="star-icon"
            />
          </button>

          {isDeleting && (
            <Loader2 className="text-muted-foreground h-3.5 w-3.5 shrink-0 animate-spin" />
          )}

          {!isDeleting && (
            <div
              className={cn(
                'shrink-0 transition-opacity duration-150',
                isActive ? 'opacity-100' : 'opacity-100 md:opacity-0 md:group-hover:opacity-100',
              )}
            >
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <button
                    onClick={(e) => e.stopPropagation()}
                    className="text-muted-foreground hover:text-foreground rounded p-1 transition-colors"
                    aria-label="更多操作"
                    title="更多操作"
                  >
                    <MoreHorizontal className="h-4 w-4" />
                  </button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-[160px]">
                  <DropdownMenuItem
                    onClick={(e) => {
                      e.stopPropagation()
                      onEdit()
                    }}
                  >
                    <Edit3 className="mr-2 h-4 w-4" />
                    编辑会话
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onClick={(e) => {
                      e.stopPropagation()
                      onCopy()
                    }}
                  >
                    <Copy className="mr-2 h-4 w-4" />
                    复制
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onClick={(e) => {
                      e.stopPropagation()
                      onStar()
                    }}
                  >
                    <Star className="mr-2 h-4 w-4" />
                    {session.starred ? '取消星标' : '星标'}
                  </DropdownMenuItem>
                  <DropdownMenuItem
                    onClick={(e) => {
                      e.stopPropagation()
                      onPin()
                    }}
                  >
                    <Pin className="mr-2 h-4 w-4" />
                    {session.pinned ? '取消置顶' : '置顶会话'}
                  </DropdownMenuItem>
                  {/* P3: 重置消息 → 刷新整个前端页面 */}
                  {onResetMessages && (
                    <DropdownMenuItem
                      onClick={(e) => {
                        e.stopPropagation()
                        onResetMessages()
                      }}
                    >
                      <RefreshCw className="mr-2 h-4 w-4" />
                      重置消息
                    </DropdownMenuItem>
                  )}
                  <DropdownMenuSeparator />
                  <DropdownMenuItem
                    onClick={(e) => {
                      e.stopPropagation()
                      onDelete()
                    }}
                    className="text-destructive focus:text-destructive"
                  >
                    <Trash2 className="mr-2 h-4 w-4" />
                    删除
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          )}
        </div>

        {/* 元信息行 · 设计稿 10px JetBrains Mono */}
        <div className="text-muted-foreground mt-0.5 truncate font-mono text-[10px] leading-none">
          {formatSessionMeta(session)}
        </div>
      </div>
    )
  },
)

function formatSessionMeta(session: Session): string {
  const updated = session.updatedAt || session.createdAt
  let timeLabel = ''
  if (updated) {
    try {
      const d = new Date(updated)
      if (!Number.isNaN(d.getTime())) {
        const now = new Date()
        const sameDay =
          d.getFullYear() === now.getFullYear() &&
          d.getMonth() === now.getMonth() &&
          d.getDate() === now.getDate()
        timeLabel = sameDay
          ? `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
          : `${d.getMonth() + 1}/${d.getDate()}`
      }
    } catch {
      /* ignore */
    }
  }
  const msgCount =
    typeof (session as { messageCount?: number }).messageCount === 'number'
      ? (session as { messageCount?: number }).messageCount
      : undefined
  if (timeLabel && msgCount !== undefined) return `${timeLabel} · ${msgCount} 条消息`
  if (timeLabel) return timeLabel
  if (msgCount !== undefined) return `${msgCount} 条消息`
  return ''
}

SessionItem.displayName = 'SessionItem'

/**
 * 会话列表组件
 * 渲染会话列表，管理删除确认对话框的状态
 */
export const SessionList = memo<SessionListProps>(
  ({
    sessions,
    activeSessionId,
    deletingSessionIds,
    onSessionClick,
    onDeleteSession,
    onEditSession,
    onCopySession,
    onStarSession,
    onPinSession,
    onResetMessages,
    className,
    itemHeight = 55,
  }) => {
    /** 删除确认对话框状态 */
    const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null)
    /** 删除操作进行中状态 */
    const [isDeleting, setIsDeleting] = useState(false)

    /**
     * 打开删除确认对话框
     */
    const handleDeleteRequest = useCallback((sessionId: string) => {
      setDeleteConfirmId(sessionId)
    }, [])

    /**
     * 取消删除，关闭确认对话框
     */
    const handleDeleteCancel = useCallback(() => {
      setDeleteConfirmId(null)
    }, [])

    /**
     * 确认删除会话
     * 调用 store 的 deleteSession 方法，完成后关闭对话框
     */
    const handleDeleteConfirm = useCallback(async () => {
      if (!deleteConfirmId) return
      setIsDeleting(true)
      try {
        await onDeleteSession(deleteConfirmId)
      } catch {
        // 错误已在 store 层处理
      } finally {
        setIsDeleting(false)
        setDeleteConfirmId(null)
      }
    }, [deleteConfirmId, onDeleteSession])

    /** 待删除会话的标题，用于确认对话框显示 */
    const deleteTargetTitle =
      sessions.find((s) => s.id === deleteConfirmId)?.title || '此会话'

    /** 使用 useMemo 缓存排序计算，避免每次渲染重复执行 filter + sort */
    const { pinnedSessions, normalSessions } = useMemo(() => {
      const sortByUpdatedAt = (a: Session, b: Session): number =>
        new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime()

      const pinned = sessions.filter((s) => s.pinned)

      return {
        pinnedSessions: pinned.sort(sortByUpdatedAt),
        normalSessions: sessions.filter((s) => !s.pinned).sort(sortByUpdatedAt),
      }
    }, [sessions])

    /** 是否存在置顶会话 */
    const hasPinned = pinnedSessions.length > 0

    /** 渲染会话项的辅助函数 */
    const renderItem = (session: Session): React.ReactNode => (
      <SessionItem
        key={session.id}
        session={session}
        isActive={activeSessionId === session.id}
        isDeleting={deletingSessionIds.has(session.id)}
        onClick={() => onSessionClick(session.id)}
        onDelete={() => handleDeleteRequest(session.id)}
        onEdit={() => onEditSession(session)}
        onCopy={() => onCopySession(session)}
        onStar={() => onStarSession(session.id)}
        onPin={() => onPinSession(session.id)}
        onResetMessages={onResetMessages ? () => onResetMessages(session.id) : undefined}
        itemHeight={itemHeight}
      />
    )

    return (
      <div className={cn('space-y-0.5', className)}>
        {/* 置顶会话分组 */}
        {hasPinned && (
          <div data-group="pinned">
            <div className="text-muted-foreground px-2 pb-1 pt-2 text-xs font-medium">
              已置顶
            </div>
            {pinnedSessions.map(renderItem)}
            <div className="border-border my-1 border-t" />
          </div>
        )}

        {/* 普通会话分组 */}
        <div data-group="normal">
          <div className="text-muted-foreground px-2 pb-1 pt-2 text-xs font-medium">
            全部会话
          </div>
          {normalSessions.map(renderItem)}
        </div>

        {/* 删除确认对话框 */}
        <Dialog
          open={!!deleteConfirmId}
          onOpenChange={(open) => !open && handleDeleteCancel()}
        >
          <DialogContent className="max-w-[360px]">
            <DialogHeader>
              <DialogTitle>确认删除</DialogTitle>
              <DialogDescription>
                确定要删除会话「{deleteTargetTitle}」吗？此操作不可撤销。删除会话将永久清除该会话中的所有消息，同时关联的数据管道执行记录和历史数据也将全部被永久删除且无法恢复。
              </DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" size="sm" onClick={handleDeleteCancel} disabled={isDeleting}>
                取消
              </Button>
              <Button
                variant="destructive"
                size="sm"
                onClick={handleDeleteConfirm}
                disabled={isDeleting}
              >
                {isDeleting ? (
                  <>
                    <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                    删除中...
                  </>
                ) : (
                  <>
                    <Trash2 className="mr-1 h-3.5 w-3.5" />
                    确认删除
                  </>
                )}
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    )
  },
)

SessionList.displayName = 'SessionList'
