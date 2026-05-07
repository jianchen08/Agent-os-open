/**
 * 会话列表组件
 *
 * 在侧边栏中渲染会话列表，每项支持：
 * - 点击切换会话
 * - Hover 时显示删除按钮（Trash 图标）
 * - 删除前弹出确认对话框（使用 shadcn/ui Dialog）
 * - 三点下拉菜单支持编辑、复制、星标、置顶、删除操作
 * - 右键上下文菜单支持重命名、置顶/取消置顶、删除操作
 * - 置顶会话分组显示（已置顶 / 全部会话）
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
import { memo, useCallback, useRef, useState } from 'react'
import { Button } from '@/components/ui/button'
import {
  ContextMenu,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
  ContextMenuTrigger,
} from '@/components/ui/context-menu'
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
import type { Session } from '@/types'

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
  /** 重命名会话回调 */
  onRenameSession?: (sessionId: string, newTitle: string) => void
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
  /** 重命名回调 */
  onRename: (newTitle: string) => void
  /** 复制回调 */
  onCopy: () => void
  /** 星标切换回调 */
  onStar: () => void
  /** 置顶切换回调 */
  onPin: () => void
  /** 列表项高度 */
  itemHeight: number
}

/**
 * 内联重命名输入组件
 */
function InlineRenameInput({
  initialTitle,
  onConfirm,
  onCancel,
}: {
  initialTitle: string
  onConfirm: (newTitle: string) => void
  onCancel: () => void
}) {
  const [title, setTitle] = useState(initialTitle)
  const inputRef = useRef<HTMLInputElement>(null)

  const handleSubmit = () => {
    const trimmed = title.trim()
    if (trimmed && trimmed !== initialTitle) {
      onConfirm(trimmed)
    } else {
      onCancel()
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === 'Enter') {
      e.preventDefault()
      handleSubmit()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      onCancel()
    }
  }

  return (
    <input
      ref={inputRef}
      type="text"
      value={title}
      onChange={(e) => setTitle(e.target.value)}
      onBlur={handleSubmit}
      onKeyDown={handleKeyDown}
      className="bg-background w-full rounded border px-1 py-0.5 text-sm outline-none focus:ring-1 focus:ring-primary"
      autoFocus
      data-testid="rename-input"
    />
  )
}

const SessionItem = memo<SessionItemProps>(
  ({
    session,
    isActive,
    isDeleting,
    onClick,
    onDelete,
    onEdit,
    onRename,
    onCopy,
    onStar,
    onPin,
    itemHeight,
  }) => {
    const [isRenaming, setIsRenaming] = useState(false)

    const handleRename = useCallback(
      (newTitle: string) => {
        setIsRenaming(false)
        onRename(newTitle)
      },
      [onRename],
    )

    const handleRenameCancel = useCallback(() => {
      setIsRenaming(false)
    }, [])

    return (
      <ContextMenu>
        <ContextMenuTrigger asChild>
          <div
            className={cn(
              'group relative flex items-center rounded-md px-2 transition-colors',
              isActive ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/50 cursor-pointer',
              isDeleting && 'pointer-events-none opacity-50',
            )}
            style={{ height: `${itemHeight}px` }}
            onClick={isRenaming ? undefined : onClick}
            role="button"
            tabIndex={0}
            aria-label={`会话: ${session.title}`}
            aria-current={isActive ? 'true' : undefined}
            data-testid="session-item"
          >
            {/* 左侧图标：置顶会话显示 Pin，普通会话显示 MessageSquare */}
            {session.pinned ? (
              <Pin
                className="text-muted-foreground mr-2 h-3.5 w-3.5 flex-shrink-0"
                data-testid="pin-icon"
              />
            ) : (
              <MessageSquare
                className="text-muted-foreground mr-2 h-3.5 w-3.5 flex-shrink-0"
                data-testid="message-icon"
              />
            )}

            {/* 标题 */}
            {isRenaming ? (
              <InlineRenameInput
                initialTitle={session.title}
                onConfirm={handleRename}
                onCancel={handleRenameCancel}
              />
            ) : (
              <span className="min-w-0 flex-1 truncate text-sm">{session.title}</span>
            )}

            {/* 星标指示器 */}
            {session.starred && (
              <Star className="mr-1 h-3 w-3 flex-shrink-0 fill-current text-status-warning" />
            )}

            {/* 正在删除加载指示 */}
            {isDeleting && (
              <Loader2 className="text-muted-foreground ml-1 h-3.5 w-3.5 flex-shrink-0 animate-spin" />
            )}

            {/* Hover 时显示的操作区域 */}
            {!isDeleting && !isRenaming && (
              <div className="flex items-center gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                {/* 删除按钮 - 直接显示 Trash 图标 */}
                <button
                  onClick={(e) => {
                    e.stopPropagation()
                    onDelete()
                  }}
                  className="text-muted-foreground hover:text-destructive rounded p-0.5 transition-colors"
                  aria-label="删除会话"
                  title="删除会话"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>

                {/* 更多操作下拉菜单 */}
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <button
                      onClick={(e) => e.stopPropagation()}
                      className="text-muted-foreground hover:text-foreground rounded p-0.5 transition-colors"
                      aria-label="更多操作"
                    >
                      <MoreHorizontal className="h-3.5 w-3.5" />
                    </button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end" className="w-[140px]">
                    <DropdownMenuItem
                      onClick={(e) => {
                        e.stopPropagation()
                        onEdit()
                      }}
                    >
                      <Edit3 className="mr-2 h-3.5 w-3.5" />
                      编辑
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onClick={(e) => {
                        e.stopPropagation()
                        onCopy()
                      }}
                    >
                      <Copy className="mr-2 h-3.5 w-3.5" />
                      复制
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onClick={(e) => {
                        e.stopPropagation()
                        onStar()
                      }}
                    >
                      <Star className="mr-2 h-3.5 w-3.5" />
                      {session.starred ? '取消星标' : '星标'}
                    </DropdownMenuItem>
                    <DropdownMenuItem
                      onClick={(e) => {
                        e.stopPropagation()
                        onPin()
                      }}
                    >
                      <Pin className="mr-2 h-3.5 w-3.5" />
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
                      <Trash2 className="mr-2 h-3.5 w-3.5" />
                      删除
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              </div>
            )}
          </div>
        </ContextMenuTrigger>

        {/* 右键上下文菜单 */}
        <ContextMenuContent className="w-[160px]">
          <ContextMenuItem
            onClick={() => {
              setIsRenaming(true)
            }}
          >
            <Edit3 className="mr-2 h-3.5 w-3.5" />
            重命名
          </ContextMenuItem>
          <ContextMenuItem
            onClick={() => {
              onPin()
            }}
          >
            <Pin className="mr-2 h-3.5 w-3.5" />
            {session.pinned ? '取消置顶' : '置顶会话'}
          </ContextMenuItem>
          <ContextMenuSeparator />
          <ContextMenuItem
            onClick={() => {
              onDelete()
            }}
            className="text-destructive focus:text-destructive"
          >
            <Trash2 className="mr-2 h-3.5 w-3.5" />
            删除
          </ContextMenuItem>
        </ContextMenuContent>
      </ContextMenu>
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
    onRenameSession,
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

    /**
     * 重命名会话（调用回调或降级为编辑）
     */
    const handleRename = useCallback(
      (session: Session, newTitle: string) => {
        if (onRenameSession) {
          onRenameSession(session.id, newTitle)
        } else {
          onEditSession(session)
        }
      },
      [onRenameSession, onEditSession],
    )

    /** 待删除会话的标题，用于确认对话框显示 */
    const deleteTargetTitle =
      sessions.find((s) => s.id === deleteConfirmId)?.title || '此会话'

    /** 按 updatedAt 降序排序的比较函数 */
    const sortByUpdatedAt = (a: Session, b: Session): number =>
      new Date(b.updatedAt).getTime() - new Date(a.updatedAt).getTime()

    /** 置顶会话列表（按 updatedAt 降序） */
    const pinnedSessions = sessions
      .filter((s) => s.pinned)
      .sort(sortByUpdatedAt)

    /** 普通会话列表（按 updatedAt 降序） */
    const normalSessions = sessions
      .filter((s) => !s.pinned)
      .sort(sortByUpdatedAt)

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
        onRename={(newTitle) => handleRename(session, newTitle)}
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
                确定要删除会话「{deleteTargetTitle}」吗？此操作不可撤销，会话中的所有消息将被永久删除。
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
