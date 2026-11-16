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

import { memo, useCallback, useMemo, useRef, useState } from 'react'
import {
  ChevronDown,
  ChevronRight,
  Copy,
  Edit3,
  Folder,
  Loader2,
  MoreHorizontal,
  Pin,
  Plus,
  RefreshCw,
  Star,
  Trash2,
} from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
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
import { useVirtualWindow } from '@/hooks/useVirtualWindow'
import { cn } from '@/lib/utils'
import { sortSessionsByRecency } from '@/stores/sessionListStore'
import {
  assignSessionToGroup,
  createSessionGroup,
  deleteSessionGroup,
  listSessionGroups,
  unassignSession,
  type SessionGroup,
} from '@/services/sessionGroups'
import { sanitizeSessionTitle } from '@/utils/sessionTitle'
import {
  groupSessionsByWorkspace,
  hasExplicitWorkspace,
  sessionRowTitleOf,
} from './workspaceTree'
import type { Session } from '@/types/models'

interface SessionListProps {
  /** 会话列表数据 */
  sessions: Session[]
  /** 当前活跃会话 ID */
  activeSessionId: string | null
  /** 正在删除中的会话 ID 集合 */
  deletingSessionIds: Set<string>
  /** 正在复制中的会话 ID 集合（行内 Loader2 pending；只读判定面） */
  copyingSessionIds?: ReadonlySet<string>
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
  /**
   * 会话树顶栏一键新建回调：以该工作空间为工作空间直接创建会话。
   * 未传时顶栏不渲染新建按钮。
   */
  onCreateInWorkspace?: (workspace: string) => void
  /** 自定义容器类名 */
  className?: string
  /** 列表项高度 */
  itemHeight?: number
}

/**
 * 窗口化阈值：超过该条数才启用虚拟滚动（renderer 内存优化项 1）。
 * 一屏最多可见约 25 条（55px × 视口上限），40 留足余量；阈值以下全量渲染，
 * 既有 DOM 结构与行为零变化。
 */
const VIRTUAL_THRESHOLD = 40

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
  /** 是否正在复制中（行内 Loader2，复制按钮连点防护） */
  isCopying?: boolean
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
  /** 打开分组对话框回调（自定义分组） */
  onGroup: () => void
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
    isCopying,
    onClick,
    onDelete,
    onEdit,
    onCopy,
    onStar,
    onPin,
    onGroup,
    onResetMessages,
    itemHeight,
  }) => {
    return (
      <div
        className={cn(
          'group relative flex flex-col justify-center rounded-lg px-2.5 transition-colors',
          isActive
            ? 'bg-[var(--ds-bg-elevated)] text-foreground ring-1 ring-[var(--ds-border-active)]'
            : 'hover:bg-[var(--ds-bg-hover)] cursor-pointer',
          (isDeleting || isCopying) && 'pointer-events-none opacity-50',
        )}
        style={{ height: `${itemHeight}px` }}
        onClick={onClick}
        role="button"
        tabIndex={0}
        aria-label={`会话: ${sessionRowTitleOf(session)}`}
        aria-current={isActive ? 'true' : undefined}
      >
        {/* 标题行 */}
        <div className="flex items-center gap-1">
          <span
            data-testid="session-title"
            className={cn(
              'min-w-0 flex-1 truncate text-[13px]',
              isActive ? 'font-medium text-foreground' : 'text-[var(--ds-text-secondary)]',
            )}
          >
            {session.pinned && (
              <Pin
                className="mr-1 inline h-3 w-3 fill-[var(--ds-accent-primary)] text-[var(--ds-accent-primary)]"
                data-testid="pin-icon"
              />
            )}
            {sessionRowTitleOf(session)}
          </span>

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

          {(isDeleting || isCopying) && (
            <Loader2
              className="text-muted-foreground h-3.5 w-3.5 shrink-0 animate-spin"
              aria-label={isCopying ? '复制中' : '删除中'}
            />
          )}

          {!isDeleting && !isCopying && (
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
                  <DropdownMenuItem
                    onClick={(e) => {
                      e.stopPropagation()
                      onGroup()
                    }}
                  >
                    <Folder className="mr-2 h-4 w-4" />
                    分组…
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
  if (!updated) return ''
  try {
    const d = new Date(updated)
    if (Number.isNaN(d.getTime())) return ''
    const now = new Date()
    const sameDay =
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate()
    return sameDay
      ? `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
      : `${d.getMonth() + 1}/${d.getDate()}`
  } catch {
    return ''
  }
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
    copyingSessionIds,
    onSessionClick,
    onDeleteSession,
    onEditSession,
    onCopySession,
    onStarSession,
    onPinSession,
    onResetMessages,
    onCreateInWorkspace,
    className,
    itemHeight = 55,
  }) => {
    /** 删除确认对话框状态 */
    const [deleteConfirmId, setDeleteConfirmId] = useState<string | null>(null)
    /** 删除操作进行中状态 */
    const [isDeleting, setIsDeleting] = useState(false)
    /** 折叠的会话树（聚合键集合）；默认全部展开 */
    const [collapsedWorkspaces, setCollapsedWorkspaces] = useState<ReadonlySet<string>>(new Set())
    /** 自定义分组：存储快照 / 折叠集合 / 分配对话框目标会话与表单态 */
    const [customGroups, setCustomGroups] = useState<SessionGroup[]>(() => listSessionGroups())
    const [collapsedCustomGroups, setCollapsedCustomGroups] = useState<ReadonlySet<string>>(new Set())
    const [groupAssignId, setGroupAssignId] = useState<string | null>(null)
    const [groupSelectedId, setGroupSelectedId] = useState<string | null>(null)
    const [groupNewName, setGroupNewName] = useState('')

    const toggleWorkspaceCollapsed = useCallback((key: string) => {
      setCollapsedWorkspaces((prev) => {
        const next = new Set(prev)
        if (next.has(key)) {
          next.delete(key)
        } else {
          next.add(key)
        }
        return next
      })
    }, [])

    const toggleCustomGroupCollapsed = useCallback((id: string) => {
      setCollapsedCustomGroups((prev) => {
        const next = new Set(prev)
        if (next.has(id)) {
          next.delete(id)
        } else {
          next.add(id)
        }
        return next
      })
    }, [])

    /** 分组对话框打开：预选当前归属组，清空新建名 */
    const openGroupDialog = useCallback((sessionId: string) => {
      setGroupAssignId(sessionId)
      setGroupSelectedId(
        listSessionGroups().find((g) => g.sessionIds.includes(sessionId))?.id ?? null,
      )
      setGroupNewName('')
    }, [])

    const refreshGroups = useCallback(() => setCustomGroups(listSessionGroups()), [])

    /** 保存分配：填了新组名即建组并移入，否则按所选组移入（都空则不可点） */
    const handleAssignConfirm = useCallback(() => {
      if (!groupAssignId) return
      const name = groupNewName.trim()
      if (name) {
        const group = createSessionGroup(name)
        assignSessionToGroup(group.id, groupAssignId)
      } else if (groupSelectedId) {
        assignSessionToGroup(groupSelectedId, groupAssignId)
      } else {
        return
      }
      refreshGroups()
      setGroupAssignId(null)
    }, [groupAssignId, groupNewName, groupSelectedId, refreshGroups])

    /** 移出当前归属组（会话回落工作空间树/平铺列表） */
    const handleUnassign = useCallback(() => {
      if (!groupAssignId) return
      unassignSession(groupAssignId)
      refreshGroups()
      setGroupAssignId(null)
    }, [groupAssignId, refreshGroups])

    /** 删组只拆组，成员会话原样保留 */
    const handleDeleteGroup = useCallback(
      (groupId: string) => {
        deleteSessionGroup(groupId)
        refreshGroups()
      },
      [refreshGroups],
    )

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

    /** 待删除会话的标题，用于确认对话框显示（展示净化，与列表行同源） */
    const deleteTargetTitle =
      sanitizeSessionTitle(
        sessions.find((s) => s.id === deleteConfirmId)?.title || '',
      ) || '此会话'

    /** 使用 useMemo 缓存分组计算，避免每次渲染重复执行 filter + sort */
    const { pinnedSessions, workspaceGroups, normalSessions, customSections } = useMemo(() => {
      const sorted = sortSessionsByRecency(sessions)
      // 置顶组恒浮顶（用户裁定 2026-11-06，回收 R10 的纯视觉标记口径）：置顶
      // 会话单独成组置于列表顶端、组内仍按最近活跃降序；先于自定义分组/工作
      // 空间树/默认空间归组摘出（单一归属：置顶优先），不受其他排序干扰
      const pinnedSessions = sorted.filter((s) => s.pinned)
      const unpinned = sorted.filter((s) => !s.pinned)
      // 自定义分组单归属占位（2026-10-18）：已分配会话不再进工作空间树/平铺
      // 列表（一处条目只出现一次）；组内成员按当前会话列表过滤，他端删除的
      // 悬挂 id 自然消失（组本身不自动删）
      const free: Session[] = []
      const byGroup = new Map<string, Session[]>()
      for (const s of unpinned) {
        const owner = customGroups.find((g) => g.sessionIds.includes(s.id))
        if (owner) {
          const arr = byGroup.get(owner.id) ?? []
          arr.push(s)
          byGroup.set(owner.id, arr)
        } else {
          free.push(s)
        }
      }
      return {
        pinnedSessions,
        // 显式工作空间 → 按目录聚合成会话树；未填（默认空间）→ 不聚合，保持平铺
        workspaceGroups: groupSessionsByWorkspace(free),
        normalSessions: free.filter((s) => !hasExplicitWorkspace(s)),
        customSections: customGroups.map((g) => ({
          group: g,
          sessions: byGroup.get(g.id) ?? [],
        })),
      }
    }, [sessions, customGroups])

    /**
     * 窗口化（超阈值启用）：本组件自持滚动容器（h-full，Sidebar 侧不再滚动），
     * 仅渲染"默认空间"组可视窗口±缓冲，占位撑出完整滚动高度；置顶组与会话树
     * 组条目少，保持全量渲染。阈值以下 DOM 结构与旧版完全一致。
     */
    const scrollRef = useRef<HTMLDivElement>(null)
    const windowRef = useRef<HTMLDivElement>(null)
    const windowed = normalSessions.length > VIRTUAL_THRESHOLD
    const { start, end } = useVirtualWindow({
      containerRef: scrollRef,
      listRef: windowRef,
      total: normalSessions.length,
      stride: itemHeight,
      enabled: windowed,
    })

    /** 渲染会话项的辅助函数 */
    const renderItem = (session: Session): React.ReactNode => (
      <SessionItem
        key={session.id}
        session={session}
        isActive={activeSessionId === session.id}
        isDeleting={deletingSessionIds.has(session.id)}
        isCopying={copyingSessionIds?.has(session.id) ?? false}
        onClick={() => onSessionClick(session.id)}
        onDelete={() => handleDeleteRequest(session.id)}
        onEdit={() => onEditSession(session)}
        onCopy={() => onCopySession(session)}
        onStar={() => onStarSession(session.id)}
        onPin={() => onPinSession(session.id)}
        onGroup={() => openGroupDialog(session.id)}
        onResetMessages={onResetMessages ? () => onResetMessages(session.id) : undefined}
        itemHeight={itemHeight}
      />
    )

    return (
      <div
        ref={scrollRef}
        data-testid="session-list-scroll"
        className={cn('min-h-0 h-full space-y-0.5 overflow-y-auto overflow-x-hidden scrollbar-thin', className)}
      >
        {/* 置顶会话分组：恒浮于列表顶端（用户裁定 2026-11-06），组内按最近
            活跃降序，不受自定义分组/工作空间树/默认空间的归组与排序干扰 */}
        {pinnedSessions.length > 0 && (
          <div data-group="pinned">
            <div className="text-muted-foreground flex items-center gap-0.5 px-2 pb-1 pt-2 text-xs font-medium">
              <span>已置顶</span>
              <span className="shrink-0 opacity-70">({pinnedSessions.length})</span>
            </div>
            {pinnedSessions.map(renderItem)}
          </div>
        )}

        {/* 自定义分组（用户策展面）：区块头=组名+计数，可折叠；「删除分组」
            只拆组不动会话；组内条目不再进工作空间树/平铺列表（单归属） */}
        {customSections.map(({ group, sessions: members }) => {
          const collapsed = collapsedCustomGroups.has(group.id)
          return (
            <div key={group.id} data-group="custom-tree" data-custom-group-id={group.id}>
              <div
                className="flex items-center gap-0.5 px-2 pb-1 pt-2 text-xs font-medium"
                data-testid="custom-group-header"
                data-group-id={group.id}
              >
                <button
                  type="button"
                  onClick={() => toggleCustomGroupCollapsed(group.id)}
                  aria-expanded={!collapsed}
                  className="text-muted-foreground hover:text-foreground flex min-w-0 flex-1 items-center gap-1 rounded text-left transition-colors"
                  data-testid="custom-group-toggle"
                >
                  {collapsed ? (
                    <ChevronRight className="h-3 w-3 shrink-0" />
                  ) : (
                    <ChevronDown className="h-3 w-3 shrink-0" />
                  )}
                  <Folder className="h-3.5 w-3.5 shrink-0 text-[var(--ds-accent-primary)]" />
                  <span className="min-w-0 truncate">{group.name}</span>
                  <span className="shrink-0 opacity-70">({members.length})</span>
                </button>
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation()
                    handleDeleteGroup(group.id)
                  }}
                  className="text-muted-foreground hover:text-foreground shrink-0 rounded p-0.5 transition-colors hover:bg-[var(--hover-overlay)]"
                  aria-label={`删除分组 ${group.name}`}
                  title="删除分组（会话保留）"
                  data-testid="custom-group-delete"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
              {!collapsed && members.map(renderItem)}
            </div>
          )
        })}

        {/* 会话树：显式工作空间按目录聚合；顶栏=空间封面（目录名+计数），可折叠，
            侧列「+」以该空间直接新建会话（onCreateInWorkspace 未传时不渲染） */}
        {workspaceGroups.map((group) => {
          const collapsed = collapsedWorkspaces.has(group.key)
          return (
            <div key={group.key} data-group="workspace-tree" data-workspace-key={group.key}>
              <div
                className="flex items-center gap-0.5 px-2 pb-1 pt-2 text-xs font-medium"
                data-testid="workspace-tree-header"
                title={`${group.workspace} · ${group.sessions.length} 个会话`}
              >
                <button
                  type="button"
                  onClick={() => toggleWorkspaceCollapsed(group.key)}
                  aria-expanded={!collapsed}
                  className="text-muted-foreground hover:text-foreground flex min-w-0 flex-1 items-center gap-1 rounded text-left transition-colors"
                  data-testid="workspace-tree-toggle"
                >
                  {collapsed ? (
                    <ChevronRight className="h-3 w-3 shrink-0" />
                  ) : (
                    <ChevronDown className="h-3 w-3 shrink-0" />
                  )}
                  <Folder className="h-3.5 w-3.5 shrink-0 text-[var(--ds-accent-primary)]" />
                  <span className="min-w-0 truncate">{group.name}</span>
                  <span className="shrink-0 opacity-70">({group.sessions.length})</span>
                </button>
                {onCreateInWorkspace && (
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation()
                      onCreateInWorkspace(group.workspace)
                    }}
                    className="text-muted-foreground hover:text-foreground shrink-0 rounded p-0.5 transition-colors hover:bg-[var(--hover-overlay)]"
                    aria-label={`在 ${group.name} 空间新建会话`}
                    title="以此空间新建会话"
                    data-testid="workspace-tree-create"
                  >
                    <Plus className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
              {!collapsed && group.sessions.map(renderItem)}
            </div>
          )
        })}

        {/* 普通会话分组（默认空间：未显式选择工作空间的会话不聚合，保持平铺） */}
        <div data-group="normal">
          <div className="text-muted-foreground px-2 pb-1 pt-2 text-xs font-medium">
            默认空间
          </div>
          {windowed ? (
            <div
              ref={windowRef}
              data-testid="session-list-window"
              style={{ height: normalSessions.length * itemHeight, position: 'relative' }}
            >
              <div style={{ position: 'absolute', top: start * itemHeight, left: 0, right: 0 }}>
                {normalSessions.slice(start, end).map(renderItem)}
              </div>
            </div>
          ) : (
            normalSessions.map(renderItem)
          )}
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

        {/* 分组对话框：选已有组单选移入，或填新组名建组并移入；已归属时可见「移出分组」 */}
        <Dialog open={!!groupAssignId} onOpenChange={(open) => !open && setGroupAssignId(null)}>
          <DialogContent className="max-w-[360px]">
            <DialogHeader>
              <DialogTitle>会话分组</DialogTitle>
              <DialogDescription>选择一个分组移入，或输入名称新建分组。</DialogDescription>
            </DialogHeader>
            <div className="max-h-56 space-y-1 overflow-y-auto" data-testid="custom-group-options">
              {customGroups.length === 0 && (
                <p className="text-muted-foreground px-1 py-2 text-xs">
                  还没有分组，输入名称新建一个。
                </p>
              )}
              {customGroups.map((g) => {
                const selected = groupSelectedId === g.id
                return (
                  <button
                    key={g.id}
                    type="button"
                    onClick={() => {
                      setGroupSelectedId(g.id)
                      setGroupNewName('')
                    }}
                    aria-pressed={selected}
                    className={cn(
                      'flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-sm transition-colors',
                      selected
                        ? 'bg-[var(--ds-bg-elevated)] text-foreground ring-1 ring-[var(--ds-border-active)]'
                        : 'hover:bg-[var(--hover-overlay)]',
                    )}
                    data-testid="custom-group-option"
                  >
                    <Folder className="h-3.5 w-3.5 shrink-0 text-[var(--ds-accent-primary)]" />
                    <span className="min-w-0 truncate">{g.name}</span>
                  </button>
                )
              })}
            </div>
            <Input
              value={groupNewName}
              onChange={(e) => setGroupNewName(e.target.value)}
              placeholder="新建分组名…"
              maxLength={30}
              data-testid="custom-group-name-input"
            />
            <DialogFooter>
              {groupSelectedId && !groupNewName.trim() && (
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={handleUnassign}
                  data-testid="custom-group-remove"
                >
                  移出分组
                </Button>
              )}
              <Button
                type="button"
                size="sm"
                onClick={handleAssignConfirm}
                disabled={!groupNewName.trim() && !groupSelectedId}
                data-testid="custom-group-save"
              >
                保存
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </div>
    )
  },
)

SessionList.displayName = 'SessionList'
