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
  MessageSquare,
  MoreHorizontal,
  Pin,
  Star,
  Trash2,
} from 'lucide-react'
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
    itemHeight,
  }) => {
    return (
      <div
        className={cn(
          'group relative flex items-center rounded-md px-2 transition-colors',
          isActive ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/50 cursor-pointer',
          isDeleting && 'pointer-events-none opacity-50',
        )}
        style={{ height: `${itemHeight}px` }}
        onClick={onClick}
        role="button"
        tabIndex={0}
        aria-label={`会话: ${session.title}`}
        aria-current={isActive ? 'true' : undefined}
      >
        {/* 左侧图标：置顶会话显示 Pin，普通会话显示 MessageSquare */}
        {session.pinned ? (
          <Pin
            className="mr-2 h-3.5 w-3.5 flex-shrink-0 fill-blue-500 text-blue-500"
            data-testid="pin-icon"
          />
        ) : (
          <MessageSquare
            className="text-muted-foreground mr-2 h-3.5 w-3.5 flex-shrink-0"
            data-testid="message-icon"
          />
        )}

        {/* 标题 */}
        <span className="min-w-0 flex-1 truncate text-sm">{session.title}</span>

        {/* 星标指示器 */}
        {session.starred && (
          <Star className="mr-1 h-3.5 w-3.5 flex-shrink-0 fill-amber-400 text-amber-400" />
        )}

        {/* 正在删除加载指示 */}
        {isDeleting && (
          <Loader2 className="text-muted-foreground ml-1 h-3.5 w-3.5 flex-shrink-0 animate-spin" />
        )}

        {/* 三点操作菜单 - hover 或活跃会话时显示 */}
        {!isDeleting && (
          <div
            className={cn(
              'ml-1 flex-shrink-0 transition-opacity duration-150',
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
    )
  },
)

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
    className,
    itemHeight = 40,
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
