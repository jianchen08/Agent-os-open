/** 任务管理面板树视图渲染部件
 *
 * 从 PipelineManagerWidget 拆出（冻结文件只许缩小）：PipelineTree 树族
 * （PipelineTree/TreeGroup/TreeChildren/EntryRow/EntryDetail）。全部 props
 * 传参、无主组件闭包依赖；主组件经 PipelineTree/PipelineTreeNode 导出消费。
 *
 * 条目行布局契约（方案 B，用户 2026-11-20 拍板 —— 勿按旧形态回退）：
 * - 行形态：[树chevron] [状态图标] [agent图标] 任务名称 … [发消息] [⋮]
 *   状态图标 = 类型+状态合一、纯展示不可点；agent 图标替代名字文字，名字只进
 *   tooltip 与详情（图标规格取 agentIcon.ts 单一真值）。
 * - 展开/收起：**点任务名称**行内展开详情（手风琴，收起不占空间），名称以
 *   hover 下划线表达可交互；**不用 chevron 做可展开提示**——树形列表里 chevron
 *   已是「展开/折叠子级」语义，一符号两义会困惑用户。
 * - 响应式：**容器宽度驱动**（行根 @container/entry + @[520px] 断点），不用设备
 *   判断。宽度不足时实时状态（状态文字/时长/token/阶段/模式）整块掉第二行。
 *   辅助信息**单份渲染**——双份 DOM 会让读屏重复朗读。
 * - 常驻操作只两个（用户实测最高频）：发消息、⋮；其余（任务现场、工作空间、
 *   复制 ID、停止/继续、删除）全部下沉 ⋮ 菜单与行内详情。
 * - 删除在任何管道状态下都可用（用户明确要求，不受状态限制）。
 */
import { useState } from 'react'
import { toast } from 'sonner'
import {
  AlertCircle,
  CheckCircle2,
  ChevronRight,
  Circle,
  Folder,
  Loader2,
  MessageSquare,
  MoreHorizontal,
  PauseCircle,
} from '@/assets/icons'
import { TabLabel } from '@/components/layout/TabLabel'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { failureClassLabel } from '@/constants/failureAttribution'
import { useTaskActing } from '@/hooks/useTaskActing'
import { entryDurationMs, formatDuration } from '@/types/activity'
import { stopReasonLabel, suspendedReasonLabel, taskStatusLabel } from '@/types/taskStatus'
import { resolveAgentIcon } from './agentIcon'
import { ModeTagBadges } from './ModePanelBadge'
import { formatLocalMinute, shortId, shortUuidsInText } from './pipelineDetailFormat'
import { entryShowsRunning, entryStatusIconKind, entryStatusVisual } from './pipelineStatusVisuals'
import type { Session } from '@/types/models'
import type { PipelineViewEntry } from '@/types/pipeline'

/** 提取条目的 token 展示值（实时 usage 优先，回退 summaries 汇总） */
function entryTokenTotal(entry: PipelineViewEntry): number | null {
  if (entry.liveUsage && entry.liveUsage.totalTokens > 0) return entry.liveUsage.totalTokens
  const tokens = entry.totalTokens
  if (tokens && typeof tokens === 'object') {
    const total = tokens.total ?? tokens.total_tokens ?? tokens.output
    if (typeof total === 'number') return total
  }
  return null
}


/** PipelineTree/TreeGroup 共用 props（树状态 + 回调族） */
export interface PipelineTreeCommonProps {
  nowMs: number
  treeOpenKeys: Set<string>
  detailOpenKeys: Set<string>
  /** 通知定位高亮的条目 key（focusTaskId 定位语义，超时自动清除） */
  locatedKey?: string | null
  onTreeToggle: (key: string) => void
  onToggleDetail: (key: string) => void
  onEntryClick: (entry: PipelineViewEntry) => void
  onAction: (
    entry: PipelineViewEntry,
    action:
      | 'pause'
      | 'resume'
      | 'delete-pipeline'
      | 'copy'
      | 'workspace'
      | 'scene'
      | 'open-folder'
      | 'delete',
  ) => void
}

export interface PipelineTreeNode {
  /** 节点 key（条目 key） */
  key: string
  /** 管道条目 */
  entry: PipelineViewEntry
  depth: number
  children: PipelineTreeNode[]
}

// ═════════════════════════════════════════════════════════════════
// 建树纯函数（自 PipelineManagerWidget 迁出：主管道锚点解析/锚点行合成）
// ═════════════════════════════════════════════════════════════════

/** 会话主管道锚点：threadId 组内 session.pipelineIds[0]，在全量条目上解析
 *  （真值主管道，不受状态筛选收窄影响）。不回退线程组最早条目——最早条目
 *  可能是兄弟任务，回退即兄弟父子假层级（用户指正 2026-09-24）；主管道
 *  条目缺席时该线程无锚点，成员落根平铺（平级可见，不互挂） */
// eslint-disable-next-line react-refresh/only-export-components -- 建树纯函数按冻结拆分归置随树部件同文件导出，非组件导出为有意为之
export function resolveThreadTop(
  pipelineEntries: PipelineViewEntry[],
  sessions: Session[],
): Map<string, string> {
  const threadTop = new Map<string, string>()
  const threadGroups = new Map<string, PipelineViewEntry[]>()
  for (const e of pipelineEntries) {
    if (!e.threadId) continue
    const list = threadGroups.get(e.threadId) ?? []
    list.push(e)
    threadGroups.set(e.threadId, list)
  }
  for (const [tid, list] of threadGroups) {
    const session = sessions.find((s) => s.id === tid)
    const mainPid = session?.pipelineIds?.[0]
    const main = mainPid ? list.find((e) => e.pipelineId === mainPid) : undefined
    if (main) threadTop.set(tid, main.key)
  }
  return threadTop
}

/** 锚点合成上下文：建树过程的登记面（视图内条目/全量条目/已建节点/根列表）
 *  与类型筛选集合——按引用传入，与原闭包同对象读写，行为等价 */
export interface PipelineTreeAnchorContext {
  entryByKey: Map<string, PipelineViewEntry>
  allEntryByKey: Map<string, PipelineViewEntry>
  nodeByKey: Map<string, PipelineTreeNode>
  roots: PipelineTreeNode[]
}

/** 锚点行合成：窄筛视图（如只看运行中）会滤掉自身不在跑的主管道条目，
 *  其锚点行从全量条目补渲染挂根——运行中的子任务平级挂其下，主管道归属
 *  可见（用户裁定 2026-09-24）。视图内已含（含稍后建行的前向引用，走
 *  childrenMap 合并）或已建的锚点直接复用 */
// eslint-disable-next-line react-refresh/only-export-components -- 同上：锚点合成纯函数随树部件同文件导出
export function ensureAnchorNode(key: string, ctx: PipelineTreeAnchorContext): string | undefined {
  if (ctx.entryByKey.has(key) || ctx.nodeByKey.has(key)) return key
  const anchorEntry = ctx.allEntryByKey.get(key)
  if (!anchorEntry) return undefined
  const node: PipelineTreeNode = { key, entry: anchorEntry, depth: 0, children: [] }
  ctx.nodeByKey.set(key, node)
  ctx.roots.push(node)
  return key
}

export function PipelineTree({
  tree,
  nowMs,
  treeOpenKeys,
  detailOpenKeys,
  locatedKey,
  onTreeToggle,
  onToggleDetail,
  onEntryClick,
  onAction,
}: {
  tree: PipelineTreeNode[]
} & PipelineTreeCommonProps) {
  // 主管道（顶层）与容器任务按状态分组：执行中 / 最近完成（子树跟随父级）。
  // 分组看子树是否含执行中成员：窄筛视图下合成的主管道锚点行（自身终态/空闲）
  // 带运行中子任务时归「执行中的管道」，不按锚点自身状态误入「最近完成」；
  // 项目登记行不是管道运行，独立成「项目」组（既非执行中也非已完成）
  const isSubtreeActive = (n: PipelineTreeNode): boolean =>
    n.entry.status === 'running'
    || n.entry.status === 'suspended'
    || n.children.some(isSubtreeActive)
  const split = (nodes: PipelineTreeNode[]) => {
    const projects: PipelineTreeNode[] = []
    const active: PipelineTreeNode[] = []
    const done: PipelineTreeNode[] = []
    for (const n of nodes) {
      if (n.entry.kind === 'project') projects.push(n)
      else if (isSubtreeActive(n)) active.push(n)
      else done.push(n)
    }
    return { projects, active, done }
  }
  const { projects, active, done } = split(tree)
  // 三组共用同一 props 族，仅 title/nodes 不同——收敛为单一渲染助手（消自克隆）
  const group = (title: string, nodes: PipelineTreeNode[]) =>
    nodes.length > 0 ? (
      <TreeGroup
        key={title}
        title={title}
        count={countNodes(nodes)}
        nodes={nodes}
        nowMs={nowMs}
        treeOpenKeys={treeOpenKeys}
        detailOpenKeys={detailOpenKeys}
        locatedKey={locatedKey}
        onTreeToggle={onTreeToggle}
        onToggleDetail={onToggleDetail}
        onEntryClick={onEntryClick}
        onAction={onAction}
      />
    ) : null
  return (
    <div className="py-1">
      {group('项目', projects)}
      {group('执行中的管道', active)}
      {group('最近完成', done)}
    </div>
  )
}

/** 统计树节点总数（含后代） */
function countNodes(nodes: PipelineTreeNode[]): number {
  let n = nodes.length
  for (const node of nodes) {
    n += countNodes(node.children)
  }
  return n
}

/** 分组：主管道顶层（对应会话层级），子任务管道直接嵌套其下；孤儿顶层平铺 */
function TreeGroup({
  title,
  count,
  nodes,
  nowMs,
  treeOpenKeys,
  detailOpenKeys,
  locatedKey,
  onTreeToggle,
  onToggleDetail,
  onEntryClick,
  onAction,
}: {
  title: string
  count: number
  nodes: PipelineTreeNode[]
} & PipelineTreeCommonProps) {
  const [collapsed, setCollapsed] = useState(false)
  return (
    <div>
      <button
        className="hover:bg-muted/50 text-muted-foreground flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-xs font-medium"
        onClick={() => setCollapsed((c) => !c)}
      >
        <ChevronRight className={`h-3.5 w-3.5 transition-transform ${collapsed ? '' : 'rotate-90'}`} />
        {title}
        <span className="text-muted-foreground/50">({count})</span>
      </button>
      {!collapsed && (
        <TreeChildren
          nodes={nodes}
          nowMs={nowMs}
          treeOpenKeys={treeOpenKeys}
          detailOpenKeys={detailOpenKeys}
          locatedKey={locatedKey}
          onTreeToggle={onTreeToggle}
          onToggleDetail={onToggleDetail}
          onEntryClick={onEntryClick}
          onAction={onAction}
        />
      )}
    </div>
  )
}

/** 递归渲染子树（主管道 → 任务条目行 → 子任务/子管道嵌套；仅在父节点树展开时挂载） */
function TreeChildren({
  nodes,
  nowMs,
  treeOpenKeys,
  detailOpenKeys,
  locatedKey,
  onTreeToggle,
  onToggleDetail,
  onEntryClick,
  onAction,
}: {
  nodes: PipelineTreeNode[]
  nowMs: number
  treeOpenKeys: Set<string>
  detailOpenKeys: Set<string>
  locatedKey?: string | null
  onTreeToggle: (key: string) => void
  onToggleDetail: (key: string) => void
  onEntryClick: (entry: PipelineViewEntry) => void
  onAction: (
    entry: PipelineViewEntry,
    action:
      | 'pause'
      | 'resume'
      | 'delete-pipeline'
      | 'copy'
      | 'workspace'
      | 'scene'
      | 'open-folder'
      | 'delete',
  ) => void
}) {
  return (
    <div>
      {nodes.map((node) => (
        <div key={node.key}>
          <EntryRow
            entry={node.entry}
            depth={node.depth}
            nowMs={nowMs}
            detailOpen={detailOpenKeys.has(node.key)}
            treeOpen={treeOpenKeys.has(node.key)}
            hasChildren={node.children.length > 0}
            locatedKey={locatedKey}
            onTreeToggle={onTreeToggle}
            onToggleDetail={onToggleDetail}
            onEntryClick={onEntryClick}
            onAction={onAction}
            orphan={node.entry.kind !== 'project' && !node.entry.threadId}
          />
          {node.children.length > 0 && treeOpenKeys.has(node.key) && (
            <TreeChildren
              nodes={node.children}
              nowMs={nowMs}
              treeOpenKeys={treeOpenKeys}
              detailOpenKeys={detailOpenKeys}
              locatedKey={locatedKey}
              onTreeToggle={onTreeToggle}
              onToggleDetail={onToggleDetail}
              onEntryClick={onEntryClick}
              onAction={onAction}
            />
          )}
        </div>
      ))}
    </div>
  )
}

/** 管道条目行（常态：agent 名/状态/名称/耗时/token；行首 chevron=树子级展开，
 *  操作区「详细信息」按钮=详情展开——两者解耦，树收起不会关详情；
 *  任务一对一绑定时此行即任务行，子任务/子管道直挂其下。
 *  行首身份位=执行 agent 名徽标（用户裁定 2026-11-06，会话/任务类型徽标退役） */
/** 辅助信息簇（实时状态）：状态/时长/token/阶段/模式——窄容器整块掉第二行。
 *
 * 自 EntryRow 抽出（函数体治理）：只读 props + 闭包变量显式传参，渲染不变。
 */
function EntryMetaChips({
  status,
  durationMs,
  tokenTotal,
  entry,
  orphan,
}: {
  status: ReturnType<typeof entryStatusVisual>
  durationMs: number | null
  tokenTotal: number | null
  entry: PipelineViewEntry
  /** 无会话归属标记（孤儿行显示「无归属」） */
  orphan?: boolean
}) {
  return (
    <>
      {status && (
        <span
          data-testid="pipeline-entry-status"
          title={`运行状态：${status.label}`}
          className={`shrink-0 rounded px-1 text-[10px] font-medium ${status.color}`}
        >
          {status.label}
        </span>
      )}
      {entry.hasRunningDescendant && !entryShowsRunning(entry) && (
        <span
          data-testid="pipeline-entry-subtask-running"
          title="下辖子管道有正在运行的（本行自身状态见左）"
          className="bg-status-info/15 text-status-info shrink-0 rounded px-1 text-[10px] font-medium"
        >
          下辖运行中
        </span>
      )}
      {entry.kind !== 'project' && durationMs != null && (
        <span
          data-testid="pipeline-entry-duration"
          className="text-muted-foreground/60 shrink-0 text-[10px] tabular-nums"
        >
          {formatDuration(durationMs)}
        </span>
      )}
      {tokenTotal != null && (
        <span
          data-testid="pipeline-entry-token"
          className="text-muted-foreground/60 shrink-0 text-[10px] tabular-nums"
        >
          {tokenTotal.toLocaleString()}
        </span>
      )}
      {entry.currentPhase && (
        <span
          className="bg-muted text-muted-foreground shrink-0 rounded px-1 text-[10px]"
          title={`当前循环体阶段：${entry.currentPhase}${entry.messageCount != null ? ` · ${entry.messageCount} 条消息` : ''}`}
        >
          {entry.currentPhase}
        </span>
      )}
      {entry.modeTags && entry.modeTags.length > 0 && <ModeTagBadges modeTags={entry.modeTags} />}
      {orphan && (
        <span className="bg-muted text-muted-foreground shrink-0 rounded px-1 text-[10px]">无归属</span>
      )}
    </>
  )
}

/** 「⋮」动作菜单（行内低频操作的唯一常驻入口，任何宽度都不依赖 hover）。
 *
 * 自 EntryRow 抽出（函数体治理）：菜单项与可用条件不变。
 */
function EntryActionsMenu({
  entry,
  open,
  onOpenChange,
  taskActing,
  onAction,
}: {
  entry: PipelineViewEntry
  open: boolean
  onOpenChange: (v: boolean) => void
  taskActing: boolean
  onAction: PipelineTreeCommonProps['onAction']
}) {
  return (
    <DropdownMenu open={open} onOpenChange={onOpenChange}>
              <DropdownMenuTrigger asChild>
                <button
                  className="text-muted-foreground hover:bg-muted hover:text-foreground flex h-6 w-6 items-center justify-center rounded transition-colors"
                  onClick={(e) => e.stopPropagation()}
                  title="更多操作"
                  aria-label="更多操作"
                  data-testid={`pipeline-entry-more-${entry.key}`}
                  tabIndex={-1}
                >
                  <MoreHorizontal className="h-4 w-4" />
                </button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end" collisionPadding={12} className="w-52">
                {/* 任务现场入口已移除（用户裁定 2026-11-20）：消息流「最新 AI 气泡状态行」
                    是唯一主入口，列表不再留重复项。 */}
                {entry.workspacePath && (
                  <DropdownMenuItem className="min-h-9" onSelect={() => onAction(entry, 'workspace')}>
                    打开工作空间
                  </DropdownMenuItem>
                )}
                {entry.kind !== 'project' && (
                  <DropdownMenuItem className="min-h-9" onSelect={() => onAction(entry, 'copy')}>
                    复制管道 ID
                  </DropdownMenuItem>
                )}
                {entry.kind === 'task' && entry.taskId && (
                  <>
                    <DropdownMenuSeparator />
                    {entry.status === 'running' && (
                      <DropdownMenuItem
                        className="min-h-9"
                        disabled={taskActing}
                        onSelect={() => onAction(entry, 'pause')}
                      >
                        停止（暂停任务）
                      </DropdownMenuItem>
                    )}
                    {entry.status === 'suspended' && (
                      <DropdownMenuItem
                        className="min-h-9"
                        disabled={taskActing}
                        onSelect={() => onAction(entry, 'resume')}
                      >
                        继续（恢复任务）
                      </DropdownMenuItem>
                    )}
                    {/* 删除：任何管道状态都可用（用户明确要求，不受状态限制） */}
                    <DropdownMenuItem
                      className="min-h-9"
                      disabled={taskActing}
                      onSelect={() => onAction(entry, 'delete-pipeline')}
                    >
                      删除管道
                    </DropdownMenuItem>
                  </>
                )}
                {entry.kind === 'project' && entry.projectId && (
                  <>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem className="min-h-9" onSelect={() => onAction(entry, 'open-folder')}>
                      打开项目文件夹
                    </DropdownMenuItem>
                    <DropdownMenuItem className="min-h-9" onSelect={() => onAction(entry, 'delete')}>
                      删除项目
                    </DropdownMenuItem>
                  </>
                )}
              </DropdownMenuContent>
    </DropdownMenu>
  )
}

function EntryRow({
  entry,
  depth,
  nowMs,
  detailOpen,
  treeOpen,
  hasChildren,
  orphan,
  locatedKey,
  onTreeToggle,
  onToggleDetail,
  onEntryClick,
  onAction,
}: {
  entry: PipelineViewEntry
  depth: number
  nowMs: number
  detailOpen: boolean
  treeOpen: boolean
  hasChildren: boolean
  orphan?: boolean
  locatedKey?: string | null
  onTreeToggle: (key: string) => void
  onToggleDetail: (key: string) => void
  onEntryClick: (entry: PipelineViewEntry) => void
  onAction: PipelineTreeCommonProps['onAction']
}) {
  // 项目登记行无运行态：不取状态视觉（渲染面按 kind 跳过状态列）
  const status = entryStatusVisual(entry)
  const iconKind = entryStatusIconKind(entry)
  const durationMs = entry.kind === 'project' ? null : entryDurationMs(entry, nowMs)
  const tokenTotal = entryTokenTotal(entry)
  const isLocated = locatedKey === entry.key
  // 行级写操作进行中（pause/resume await 窗口）：真值源 = store 级 actingTaskIds
  const taskActing = useTaskActing(entry.taskId)
  /** 「⋮」动作菜单开合（行内低频操作的唯一常驻入口，任何宽度都不依赖 hover） */
  const [actionsOpen, setActionsOpen] = useState(false)
  /** agent 图标规格（emoji 文本 / 内置组件 / 缺省兜底；未配置不留空） */
  const agentIcon = resolveAgentIcon(entry.agentIcon)

  /** 辅助信息簇（实时状态）：宽容器同行右侧，窄容器整块掉第二行——
   *  同一份数据两处渲染、由容器查询切换显隐（不用设备判断） */

  return (
    <div>
      {/* 行容器（@container/entry）：辅助信息显隐由**本行宽度**驱动，
          与设备类型无关——桌面窄窗口同样掉第二行 */}
      <div
        data-entry-key={entry.key}
        data-located={isLocated || undefined}
        className={`@container/entry group/entry transition-colors ${
          entry.kind === 'project' ? '' : 'hover:bg-muted cursor-pointer'
        } ${isLocated ? 'bg-primary/10' : ''}`}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
      >
        {/* 主行：树 chevron + 状态图标 + agent 图标 + 名称 + 辅助信息(宽) + 两个常驻操作。
            项目行同走点名称展开详情（EntryDetail 项目分支：项目 ID/路径/登记时间——
            方案 B 8 条无项目行例外，双重排除曾把项目详情卡打成不可达死代码） */}
        <div
          className="flex flex-wrap cursor-pointer items-center gap-x-1.5 gap-y-0.5 py-1.5 pr-2"
          onClick={() => onToggleDetail(entry.key)}
        >
          {hasChildren ? (
            <button
              className="text-muted-foreground hover:text-foreground flex h-5 w-5 shrink-0 items-center justify-center rounded transition-transform"
              onClick={(e) => {
                e.stopPropagation()
                onTreeToggle(entry.key)
              }}
              title="展开/收起子管道"
              aria-label="展开或收起子管道"
              tabIndex={-1}
            >
              <ChevronRight className={`h-3.5 w-3.5 transition-transform ${treeOpen ? 'rotate-90' : ''}`} />
            </button>
          ) : (
            <span className="h-5 w-5 shrink-0" />
          )}

          {/* 状态图标（类型+状态合一、纯展示不可点）：颜色取状态视觉单一真值 */}
          <span
            data-testid="pipeline-entry-status-icon"
            data-status-kind={iconKind ?? 'project'}
            title={
              entry.kind === 'project'
                ? '项目'
                : `状态：${status?.label ?? '未知'}（点击行内任务名称查看全部信息）`
            }
            aria-label={entry.kind === 'project' ? '项目' : `状态：${status?.label ?? '未知'}`}
            className={`flex h-5 w-5 shrink-0 items-center justify-center ${
              entry.kind === 'project' ? 'text-status-success' : (status?.color ?? 'text-muted-foreground')
            }`}
          >
            {entry.kind === 'project' ? (
              <Folder className="h-3.5 w-3.5" />
            ) : iconKind === 'running' ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : iconKind === 'paused' ? (
              <PauseCircle className="h-3.5 w-3.5" />
            ) : iconKind === 'failed' ? (
              <AlertCircle className="h-3.5 w-3.5" />
            ) : iconKind === 'completed' ? (
              <CheckCircle2 className="h-3.5 w-3.5" />
            ) : (
              <Circle className="h-3.5 w-3.5" />
            )}
          </span>

          {/* agent 图标（替代原 agent 名字文字）：名字转悬停提示，未配置走缺省 */}
          {entry.kind !== 'project' && (
            <span
              data-testid="pipeline-entry-agent-icon"
              title={entry.agentLabel ?? '未知 agent'}
              aria-label={entry.agentLabel ?? '未知 agent'}
              className="text-muted-foreground flex h-5 w-5 shrink-0 items-center justify-center text-[13px] leading-none"
            >
              {agentIcon.kind === 'emoji' ? (
                <span aria-hidden="true">{agentIcon.value}</span>
              ) : (
                <agentIcon.Icon className="h-3.5 w-3.5" />
              )}
            </span>
          )}

          {/* 任务名称（主体 + 详情展开入口）：可交互样式用 hover 下划线表达，
              不使用 chevron——chevron 在树形列表里已是「展开子级」的语义 */}
          <button
            type="button"
            data-testid="pipeline-entry-title"
            title={`${entry.name}（点击展开/收起详细信息）`}
            aria-expanded={detailOpen}
            onClick={(e) => {
              e.stopPropagation()
              onToggleDetail(entry.key)
            }}
            className="text-foreground/90 hover:text-foreground min-w-0 flex-1 truncate text-left text-sm hover:underline hover:decoration-dotted hover:underline-offset-2"
          >
            {entry.name}
          </button>

          {/* 辅助信息（单份渲染）：窄容器 basis-full 独占一行（掉到第二行），
              宽容器 basis-auto 跟随主行——纯 CSS 换行，不做双份 DOM（重复渲染会
              让读屏重复朗读、文本查询重复命中） */}
          <span
            data-testid="pipeline-entry-meta"
            className="order-last flex min-w-0 basis-full flex-wrap items-center gap-1.5 pl-11 @[520px]/entry:order-none @[520px]/entry:basis-auto @[520px]/entry:shrink-0 @[520px]/entry:pl-0"
          >
            <EntryMetaChips
              status={status}
              durationMs={durationMs}
              tokenTotal={tokenTotal}
              entry={entry}
              orphan={orphan}
            />
          </span>

          {/* 常驻操作（用户实测最高频的两个；其余全部下沉详情与「⋮」） */}
          <span data-testid="pipeline-entry-actions" className="flex shrink-0 items-center gap-0.5">
            {entry.kind !== 'project' && (
              <button
                className="bg-primary/15 text-primary hover:bg-primary/25 flex h-6 w-6 items-center justify-center rounded transition-colors"
                onClick={(e) => {
                  e.stopPropagation()
                  onEntryClick(entry)
                }}
                title="发消息（打开对话）"
                aria-label="发消息"
                tabIndex={-1}
                data-testid={`pipeline-entry-message-${entry.key}`}
              >
                <MessageSquare className="h-3.5 w-3.5" />
              </button>
            )}
            <EntryActionsMenu
              entry={entry}
              open={actionsOpen}
              onOpenChange={setActionsOpen}
              taskActing={taskActing}
              onAction={onAction}
            />
          </span>
        </div>
      </div>
      {detailOpen && (
        <EntryDetail entry={entry} depth={depth} onAction={onAction} taskActing={taskActing} />
      )}
    </div>
  )
}

/** 详情行种类：text=普通值；id=可点复制的 ID（显示前 8 位，复制完整值）；
 *  time=ISO 时间戳（渲染本地「YYYY-MM-DD HH:mm」）；longtext=可折叠长文（错误） */
type DetailRowKind = 'text' | 'id' | 'time' | 'longtext'

/** 复制详情完整值：走既有 toast 面（与 MessageActions 同口径），失败显式报错不静默 */
function copyDetailValue(label: string, value: string) {
  navigator.clipboard
    .writeText(value)
    .then(() => toast.success(`已复制${label}完整值`))
    .catch(() => toast.error('复制失败'))
}

/** 错误长文折叠（P1-4，MuMu 实测 shots/02 错误墙占满整屏）：默认 2 行截断
 *  （line-clamp），「展开」看全文，「收起」回收。 */
function CollapsibleDetailText({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <span className="min-w-0 flex-1">
      <span
        data-testid="pipeline-detail-error-text"
        className={`text-foreground/80 block break-all font-mono text-[10px] ${
          expanded ? '' : 'line-clamp-2'
        }`}
      >
        {text}
      </span>
      <button
        type="button"
        className="text-primary mt-0.5 inline-flex h-8 min-w-11 items-center justify-start text-[10px]"
        onClick={() => setExpanded((v) => !v)}
        data-testid={expanded ? 'pipeline-detail-error-collapse' : 'pipeline-detail-error-expand'}
        aria-expanded={expanded}
      >
        {expanded ? '收起' : '展开'}
      </button>
    </span>
  )
}

/** 展开详情（行内手风琴，点任务名称触发）：承接全部低频信息与操作。
 *
 * 用户拍板的方案 B：列表行只留「状态图标 + agent 图标 + 名称 + 发消息 + ⋮」，
 * 其余全部收进来——完整 ID（点复制）、归属、时间、agent 全名、模式、阶段、
 * 消息条数、token 明细、错误全文、停止/失败归因。
 *
 * 操作条（data-testid=pipeline-detail-actions）：停止/继续（按状态出现）、
 * **删除（任何状态都可用，用户明确要求）**、复制完整管道 ID。
 */
function EntryDetail({
  entry,
  depth,
  onAction,
  taskActing,
}: {
  entry: PipelineViewEntry
  depth: number
  onAction: PipelineTreeCommonProps['onAction']
  taskActing?: boolean
}) {
  const tokenTotal = entryTokenTotal(entry)
  // 详情标题：管道名 + 运行状态（面板自带标题，独立读时不必回看行首）
  const status = entryStatusVisual(entry)
  const rows: Array<{ kind: DetailRowKind; label: string; value: string }> =
    entry.kind === 'project'
      ? [
          ...(entry.projectId ? [{ kind: 'id' as const, label: '项目 ID', value: entry.projectId }] : []),
          ...(entry.workspacePath ? [{ kind: 'text' as const, label: '路径', value: entry.workspacePath }] : []),
          { kind: 'time' as const, label: '登记时间', value: entry.startedAt },
        ]
      : [
          { kind: 'id' as const, label: '管道 ID', value: entry.pipelineId || entry.key },
          ...(entry.runId ? [{ kind: 'id' as const, label: '运行 ID', value: entry.runId }] : []),
          entry.sessionTitle
            ? {
                kind: 'id' as const,
                label: '归属',
                value: `${entry.sessionTitle}（${entry.threadId}）`,
              }
            : { kind: 'text' as const, label: '归属', value: '无归属' },
          { kind: 'time' as const, label: '开始', value: entry.startedAt },
        ]
  if (entry.endedAt) rows.push({ kind: 'time', label: '结束', value: entry.endedAt })
  // Agent 全名（行内已改图标，全名只在此可见——用户定的「图标替代文字」配套）
  if (entry.agentLabel) rows.push({ kind: 'text', label: 'Agent', value: entry.agentLabel })
  // 模式（行内只在宽容器显示徽标，全量标签在此）
  if (entry.modeTags && entry.modeTags.length > 0)
    rows.push({ kind: 'text', label: '模式', value: entry.modeTags.join('、') })
  if (entry.taskStatus)
    rows.push({ kind: 'text', label: '任务状态', value: `${taskStatusLabel(entry.taskStatus)}（${entry.taskStatus}）` })
  if (entry.stateStatus) rows.push({ kind: 'text', label: 'State 状态', value: entry.stateStatus })
  if (entry.stateEnded !== undefined)
    rows.push({ kind: 'text', label: '已结束', value: entry.stateEnded ? '是' : '否' })
  if (entry.currentPhase) rows.push({ kind: 'text', label: '当前阶段', value: entry.currentPhase })
  if (entry.messageCount != null)
    rows.push({ kind: 'text', label: '消息条数', value: String(entry.messageCount) })
  if (entry.rawError) rows.push({ kind: 'longtext', label: '错误', value: entry.rawError })
  if (entry.stopReason) rows.push({ kind: 'text', label: '停止原因', value: stopReasonLabel(entry.stopReason) })
  if (entry.suspendedReason)
    rows.push({ kind: 'text', label: '挂起原因', value: suspendedReasonLabel(entry.suspendedReason) })
  if (entry.failureClass || entry.failureReason) {
    const parts = [failureClassLabel(entry.failureClass ?? ''), entry.failureReason]
    rows.push({ kind: 'text', label: '失败归因', value: parts.filter(Boolean).join('：') })
  }
  if (entry.totalTokens && typeof entry.totalTokens === 'object') {
    const t = entry.totalTokens
    const parts = ['input', 'output', 'total']
      .filter((k) => typeof t[k] === 'number')
      .map((k) => `${k}=${t[k]}`)
    if (parts.length > 0) rows.push({ kind: 'text', label: 'Token 汇总', value: parts.join(' · ') })
  }
  if (tokenTotal != null)
    rows.push({ kind: 'text', label: 'Token 实时', value: tokenTotal.toLocaleString() })

  /** 详情操作条按钮统一样式 */
  const actionBtn =
    'inline-flex min-h-7 items-center rounded border border-border/60 px-2 text-[11px] transition-colors hover:bg-muted'

  return (
    <div
      className="border-muted/50 bg-muted/20 border-l-2 py-1.5 pr-3"
      style={{ paddingLeft: `${depth * 16 + 28}px` }}
      data-testid="pipeline-detail-panel"
    >
      <div
        data-testid="pipeline-detail-header"
        className="border-border/40 mb-1 flex min-w-0 items-center gap-1.5 border-b pb-1"
      >
        <span className="flex min-w-0 flex-1 items-center" title={entry.name}>
          <TabLabel title={entry.name} className="text-foreground/90 text-xs font-medium" />
        </span>
        {status && (
          <span
            data-testid="pipeline-detail-status"
            title={`运行状态：${status.label}`}
            className={`shrink-0 rounded px-1 text-[10px] font-medium ${status.color}`}
          >
            {status.label}
          </span>
        )}
        {entry.hasRunningDescendant && !entryShowsRunning(entry) && (
          <span
            data-testid="pipeline-detail-subtask-running"
            title="下辖子管道有正在运行的（本行自身状态见左侧标签）"
            className="bg-status-info/15 text-status-info shrink-0 rounded px-1 text-[10px] font-medium"
          >
            下辖运行中
          </span>
        )}
      </div>

      {/* 操作条：低频动作的可发现承载面（用户诉求核心：不必预先知道去哪找） */}
      {entry.kind !== 'project' && (
        <div
          data-testid="pipeline-detail-actions"
          className="mb-1 flex flex-wrap items-center gap-1"
        >
          {entry.kind === 'task' && entry.taskId && entry.status === 'running' && (
            <button
              type="button"
              className={actionBtn}
              disabled={taskActing}
              onClick={() => onAction(entry, 'pause')}
              data-testid="pipeline-detail-pause"
            >
              停止
            </button>
          )}
          {entry.kind === 'task' && entry.taskId && entry.status === 'suspended' && (
            <button
              type="button"
              className={actionBtn}
              disabled={taskActing}
              onClick={() => onAction(entry, 'resume')}
              data-testid="pipeline-detail-resume"
            >
              继续
            </button>
          )}
          <button
            type="button"
            className={actionBtn}
            onClick={() => onAction(entry, 'copy')}
            data-testid="pipeline-detail-copy-id"
          >
            复制完整 ID
          </button>
          {/* 删除：任何管道状态都可用（用户明确要求：不受状态限制） */}
          {entry.kind === 'task' && entry.taskId && (
            <button
              type="button"
              className={`${actionBtn} text-status-error hover:bg-status-error/10`}
              disabled={taskActing}
              onClick={() => onAction(entry, 'delete-pipeline')}
              data-testid="pipeline-detail-delete"
            >
              删除
            </button>
          )}
        </div>
      )}

      {rows.map((row) => (
        <div key={row.label} className="flex gap-2 py-0.5 text-[11px]">
          <span className="text-muted-foreground/60 w-16 shrink-0">{row.label}</span>
          {row.kind === 'id' && row.value ? (
            <button
              type="button"
              className="text-foreground/80 min-w-0 flex-1 cursor-pointer break-all text-left font-mono text-[10px] underline decoration-dotted underline-offset-2"
              title="点击复制完整值"
              aria-label={`复制${row.label}完整值`}
              data-testid={`pipeline-detail-copy-${row.label}`}
              onClick={() => copyDetailValue(row.label, row.value)}
            >
              {row.label === '归属' ? shortUuidsInText(row.value) : shortId(row.value)}
            </button>
          ) : row.kind === 'longtext' ? (
            <CollapsibleDetailText text={row.value} />
          ) : (
            <span className="text-foreground/80 min-w-0 flex-1 break-all font-mono text-[10px]">
              {row.kind === 'time' ? formatLocalMinute(row.value) : row.value}
            </span>
          )}
        </div>
      ))}
      {entry.kind === 'task' && entry.progress !== undefined && (
        <div className="mt-1 flex items-center gap-2">
          <div className="bg-muted h-1.5 flex-1 overflow-hidden rounded-full">
            <div
              className="bg-status-info h-full rounded-full transition-all duration-500"
              style={{ width: `${Math.max(0, Math.min(100, entry.progress))}%` }}
            />
          </div>
          <span className="text-muted-foreground shrink-0 text-[10px] tabular-nums">
            {entry.progress}%
          </span>
        </div>
      )}
    </div>
  )
}
