/** 任务管理面板树视图/列表视图渲染部件
 *
 * 从 PipelineManagerWidget 拆出（冻结文件只许缩小）：PipelineTree 树族
 * （PipelineTree/TreeGroup/TreeChildren/EntryRow/EntryDetail）与
 * PipelineTable 列表视图。全部 props 传参、无主组件闭包依赖；
 * 主组件经 PipelineTree/PipelineTable/PipelineTreeNode 三导出消费。
 */
import React, { useState } from 'react'
import {
  ChevronRight,
  XCircle,
  PauseCircle,
  PlayCircle,
  CopyIcon,
  FolderOpen,
  InfoIcon,
  MessageSquare,
  Trash2,
} from '@/assets/icons'
import { statusIcon } from './pipelineStatusVisuals'
import { ModePanelBadge } from './ModePanelBadge'
import { entryDurationMs, formatDuration } from '@/types/activity'
import { taskStatusLabel } from '@/types/taskStatus'
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
    action: 'pause' | 'resume' | 'cancel' | 'copy' | 'workspace' | 'open-folder' | 'delete',
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
  // 主管道（顶层）与容器任务按状态分组：执行中 / 最近完成（子树跟随父级）；
  // 项目登记行不是管道运行，独立成「项目」组（既非执行中也非已完成）
  const isActiveNode = (n: PipelineTreeNode): boolean =>
    n.entry.status === 'running' || n.entry.status === 'suspended'
  const split = (nodes: PipelineTreeNode[]) => {
    const projects: PipelineTreeNode[] = []
    const active: PipelineTreeNode[] = []
    const done: PipelineTreeNode[] = []
    for (const n of nodes) {
      if (n.entry.kind === 'project') projects.push(n)
      else if (isActiveNode(n)) active.push(n)
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
      {tree.length === 0 && (
        <div className="text-muted-foreground px-4 py-6 text-center text-xs">
          暂无管道运行记录
        </div>
      )}
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
        className="hover:bg-accent/50 text-muted-foreground flex w-full items-center gap-1.5 px-3 py-1.5 text-left text-xs font-medium"
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
    action: 'pause' | 'resume' | 'cancel' | 'copy' | 'workspace' | 'open-folder' | 'delete',
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

/** 管道条目行（常态：类型/状态/名称/agent/耗时/token；行首 chevron=树子级展开，
 *  操作区「详细信息」按钮=详情展开——两者解耦，树收起不会关详情；
 *  任务一对一绑定时此行即任务行，子任务/子管道直挂其下） */
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
  onAction: (
    entry: PipelineViewEntry,
    action: 'pause' | 'resume' | 'cancel' | 'copy' | 'workspace' | 'open-folder' | 'delete',
  ) => void
}) {
  // 项目登记行无运行态：不取状态图标（渲染面按 kind 跳过状态/耗时/token 列）
  const status = entry.kind === 'project' ? null : statusIcon(entry.status ?? '')
  const durationMs = entry.kind === 'project' ? null : entryDurationMs(entry, nowMs)
  const tokenTotal = entryTokenTotal(entry)
  const isLocated = locatedKey === entry.key

  return (
    <div>
      <div
        data-entry-key={entry.key}
        data-located={isLocated || undefined}
        className={`group flex items-center gap-1.5 py-1.5 pr-2 transition-colors ${
          entry.kind === 'project' ? '' : 'hover:bg-accent cursor-pointer'
        } ${isLocated ? 'bg-primary/10' : ''}`}
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
        onClick={entry.kind === 'project' ? undefined : () => onEntryClick(entry)}
        title={entry.kind === 'project' ? undefined : '打开对话标签'}
      >
        {/* 树子级展开 chevron（仅有子级时渲染；叶子行占位对齐） */}
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
        {/* 类型徽标 */}
        <span
          className={`shrink-0 rounded px-1 py-0 text-[10px] font-medium ${
            entry.kind === 'task'
              ? 'bg-status-warning/15 text-status-warning'
              : entry.kind === 'project'
                ? 'bg-status-success/15 text-status-success'
                : 'bg-primary/10 text-primary/80'
          }`}
        >
          {entry.kind === 'task' ? '任务' : entry.kind === 'project' ? '项目' : '会话'}
        </span>
        {orphan && (
          <span className="shrink-0 rounded bg-muted px-1 py-0 text-[10px] text-muted-foreground">
            无归属
          </span>
        )}
        {/* 状态图标（title 带原始状态——4 态映射的细态不丢；项目登记行无运行态） */}
        {status && (
          <span
            className="shrink-0"
            title={`运行状态：${status.label}${entry.taskStatus ? ` · 任务原始状态：${entry.taskStatus}` : ''}${entry.stateStatus ? ` · state 真值：${entry.stateStatus}` : ''}`}
          >
            {status.icon}
          </span>
        )}
        {/* 名称 */}
        <span className="text-foreground/90 min-w-0 flex-1 truncate text-sm">{entry.name}</span>
        {/* 任务态（两态模型：任务条目独立展示任务域状态，与运行态图标分离；
            非任务条目无 taskStatus 不渲染。原始值进 title 不占版面） */}
        {entry.taskStatus && (
          <span
            className="bg-muted text-muted-foreground hidden shrink-0 rounded px-1 py-0 text-[10px] md:inline"
            title={`任务状态：${taskStatusLabel(entry.taskStatus)}（原始值 ${entry.taskStatus}）`}
          >
            任务:{taskStatusLabel(entry.taskStatus)}
          </span>
        )}
        {/* 循环体阶段（内核 state.current_phase：init/main/exit…） */}
        {entry.currentPhase && (
          <span
            className="bg-muted text-muted-foreground hidden shrink-0 rounded px-1 text-[10px] sm:inline"
            title={`当前循环体阶段：${entry.currentPhase}${entry.messageCount != null ? ` · ${entry.messageCount} 条消息` : ''}`}
          >
            {entry.currentPhase}
          </span>
        )}
        {/* 模式徽标插槽（§5.0 观测链）：state.mode 有键且 registry 查得到
            面板映射才渲染（§5.3 查不到映射=不渲染）；点击打开模式面板 */}
        {entry.mode && <ModePanelBadge mode={entry.mode} />}
        {/* agent */}
        {entry.agentName && (
          <span className="bg-primary/10 text-primary/70 hidden shrink-0 rounded px-1.5 py-0 text-[10px] sm:inline">
            {entry.agentName}
          </span>
        )}
        {/* 状态标签（项目登记行无运行态不渲染） */}
        {status && (
          <span className={`shrink-0 rounded px-1 text-[10px] font-medium ${status.color}`}>
            {status.label}
          </span>
        )}
        {/* 耗时（项目登记行非运行，无耗时语义） */}
        {entry.kind !== 'project' && (
          <span className="text-muted-foreground/60 hidden shrink-0 text-[10px] tabular-nums md:inline">
            {formatDuration(durationMs)}
          </span>
        )}
        {/* token */}
        <span className="text-muted-foreground/60 shrink-0 text-[10px] tabular-nums">
          {tokenTotal != null ? tokenTotal.toLocaleString() : '--'}
        </span>
        {/* 操作按钮 */}
        <div className="flex shrink-0 items-center gap-0.5">
          {/* 详细信息（独立于树展开：树收起不关详情，信息停留显示） */}
          <button
            className={`${
              detailOpen
                ? 'bg-primary/20 text-primary'
                : 'text-muted-foreground hover:bg-accent hover:text-foreground'
            } flex h-6 w-6 items-center justify-center rounded transition-colors`}
            onClick={(e) => {
              e.stopPropagation()
              onToggleDetail(entry.key)
            }}
            title="详细信息（展开/收起详情，不受树展开影响）"
            aria-label="切换详细信息"
            tabIndex={-1}
          >
            <InfoIcon className="h-3.5 w-3.5" />
          </button>
          {entry.kind !== 'project' && (
            <button
              className="bg-primary/15 text-primary hover:bg-primary/25 flex h-6 w-6 items-center justify-center rounded transition-colors"
              onClick={(e) => {
                e.stopPropagation()
                onEntryClick(entry)
              }}
              title="打开对话"
              aria-label="打开对话"
              tabIndex={-1}
            >
              <MessageSquare className="h-3.5 w-3.5" />
            </button>
          )}
          {entry.kind === 'project' && entry.projectId && (
            <button
              className="bg-primary/15 text-primary hover:bg-primary/25 flex h-6 w-6 items-center justify-center rounded transition-colors"
              onClick={(e) => {
                e.stopPropagation()
                onAction(entry, 'open-folder')
              }}
              title="在系统文件管理器中打开项目文件夹"
              aria-label="打开文件夹"
              tabIndex={-1}
            >
              <FolderOpen className="h-3.5 w-3.5" />
            </button>
          )}
          {entry.kind === 'project' && entry.projectId && (
            <button
              className="text-muted-foreground hover:bg-status-error/15 hover:text-status-error flex h-6 w-6 items-center justify-center rounded transition-colors"
              onClick={(e) => {
                e.stopPropagation()
                onAction(entry, 'delete')
              }}
              title="删除项目"
              aria-label="删除项目"
              tabIndex={-1}
            >
              <Trash2 className="h-3.5 w-3.5" />
            </button>
          )}
          <button
            className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-6 w-6 items-center justify-center rounded transition-colors"
            onClick={(e) => {
              e.stopPropagation()
              onAction(entry, 'copy')
            }}
            title="复制管道 ID"
            aria-label="复制管道 ID"
            tabIndex={-1}
          >
            <CopyIcon className="h-3.5 w-3.5" />
          </button>
          {entry.kind === 'task' && entry.taskId && entry.status === 'running' && (
            <>
              <button
                className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-6 w-6 items-center justify-center rounded transition-colors"
                onClick={(e) => {
                  e.stopPropagation()
                  onAction(entry, 'pause')
                }}
                title="暂停任务"
                tabIndex={-1}
              >
                <PauseCircle className="h-3.5 w-3.5" />
              </button>
              <button
                className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-6 w-6 items-center justify-center rounded transition-colors"
                onClick={(e) => {
                  e.stopPropagation()
                  onAction(entry, 'cancel')
                }}
                title="取消任务"
                tabIndex={-1}
              >
                <XCircle className="h-3.5 w-3.5" />
              </button>
            </>
          )}
          {entry.kind === 'task' && entry.taskId && entry.status === 'suspended' && (
            <button
              className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-6 w-6 items-center justify-center rounded transition-colors"
              onClick={(e) => {
                e.stopPropagation()
                onAction(entry, 'resume')
              }}
              title="恢复任务"
              tabIndex={-1}
            >
              <PlayCircle className="h-3.5 w-3.5" />
            </button>
          )}
          {entry.workspacePath && (
            <button
              className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-6 w-6 items-center justify-center rounded transition-colors"
              onClick={(e) => {
                e.stopPropagation()
                onAction(entry, 'workspace')
              }}
              title={`打开工作空间: ${entry.workspacePath}`}
              tabIndex={-1}
            >
              <FolderOpen className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>
      {detailOpen && <EntryDetail entry={entry} depth={depth} />}
    </div>
  )
}

/** 展开详情：管道 ID/归属/时间/token 明细/进度；项目登记行走项目字段
 *  （项目 ID/路径/登记时间——项目没有管道，不渲染管道 ID/运行 ID/归属） */
function EntryDetail({ entry, depth }: { entry: PipelineViewEntry; depth: number }) {
  const tokenTotal = entryTokenTotal(entry)
  const rows: Array<[string, string]> =
    entry.kind === 'project'
      ? [
          ['项目 ID', entry.projectId ?? ''],
          ...(entry.workspacePath ? ([['路径', entry.workspacePath]] as Array<[string, string]>) : []),
          ['登记时间', entry.startedAt],
        ]
      : [
          ['管道 ID', entry.pipelineId || entry.key],
          ['运行 ID', entry.runId],
          ['归属', entry.sessionTitle ? `${entry.sessionTitle}（${entry.threadId}）` : '无归属'],
          ['开始', entry.startedAt],
        ]
  if (entry.endedAt) rows.push(['结束', entry.endedAt])
  if (entry.agentName) rows.push(['Agent', entry.agentName])
  // state 真值关键数据（任务条目上的关键信息不缺位）
  if (entry.taskStatus) rows.push(['任务状态', `${taskStatusLabel(entry.taskStatus)}（${entry.taskStatus}）`])
  if (entry.stateStatus) rows.push(['State 状态', entry.stateStatus])
  if (entry.stateEnded !== undefined) rows.push(['已结束', entry.stateEnded ? '是' : '否'])
  if (entry.currentPhase) rows.push(['当前阶段', entry.currentPhase])
  if (entry.messageCount != null) rows.push(['消息条数', String(entry.messageCount)])
  if (entry.rawError) rows.push(['错误', entry.rawError])
  if (entry.totalTokens && typeof entry.totalTokens === 'object') {
    const t = entry.totalTokens
    const parts = ['input', 'output', 'total']
      .filter((k) => typeof t[k] === 'number')
      .map((k) => `${k}=${t[k]}`)
    if (parts.length > 0) rows.push(['Token 汇总', parts.join(' · ')])
  }
  if (tokenTotal != null) rows.push(['Token 实时', tokenTotal.toLocaleString()])

  return (
    <div
      className="border-muted/50 bg-muted/20 border-l-2 py-1.5 pl-2 pr-3"
      style={{ paddingLeft: `${depth * 16 + 20}px` }}
    >
      {rows.map(([k, v]) => (
        <div key={k} className="flex gap-2 py-0.5 text-[11px]">
          <span className="text-muted-foreground/60 w-16 shrink-0">{k}</span>
          <span className="text-foreground/80 min-w-0 flex-1 break-all font-mono text-[10px]">{v}</span>
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

// ═════════════════════════════════════════════════════════════════
// 列表视图：平铺表格（类型/名称/归属/状态/耗时/token/操作）
// ═════════════════════════════════════════════════════════════════

export function PipelineTable({
  entries,
  nowMs,
  expandedKeys,
  locatedKey,
  onToggle,
  onEntryClick,
  onAction,
}: {
  entries: PipelineViewEntry[]
  nowMs: number
  expandedKeys: Set<string>
  /** 通知定位高亮的条目 key（focusTaskId 定位语义，超时自动清除） */
  locatedKey?: string | null
  onToggle: (key: string) => void
  onEntryClick: (entry: PipelineViewEntry) => void
  onAction: (
    entry: PipelineViewEntry,
    action: 'pause' | 'resume' | 'cancel' | 'copy' | 'workspace' | 'open-folder' | 'delete',
  ) => void
}) {
  if (entries.length === 0) {
    return (
      <div className="px-4 py-6 text-center">
        <p className="text-muted-foreground text-xs">暂无管道运行记录</p>
      </div>
    )
  }
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-left">
            <th className="text-muted-foreground px-3 py-1.5 text-[11px] font-medium">类型</th>
            <th className="text-muted-foreground px-3 py-1.5 text-[11px] font-medium">名称</th>
            <th className="text-muted-foreground hidden px-3 py-1.5 text-[11px] font-medium sm:table-cell">归属</th>
            <th className="text-muted-foreground hidden px-3 py-1.5 text-[11px] font-medium lg:table-cell">Agent</th>
            <th className="text-muted-foreground px-3 py-1.5 text-[11px] font-medium">状态</th>
            <th className="text-muted-foreground hidden px-3 py-1.5 text-[11px] font-medium md:table-cell">耗时</th>
            <th className="text-muted-foreground px-3 py-1.5 text-right text-[11px] font-medium">Token</th>
            <th className="text-muted-foreground px-3 py-1.5 text-[11px] font-medium">操作</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => {
            // 项目登记行无运行态：状态/耗时列显 —（列位保留对齐）
            const status = entry.kind === 'project' ? null : statusIcon(entry.status ?? '')
            const durationMs = entry.kind === 'project' ? null : entryDurationMs(entry, nowMs)
            const tokenTotal = entryTokenTotal(entry)
            const isLocated = locatedKey === entry.key
            return (
              <React.Fragment key={entry.key}>
                <tr
                  data-entry-key={entry.key}
                  data-located={isLocated || undefined}
                  className={`border-b last:border-b-0 ${isLocated ? 'bg-primary/10' : ''} ${
                    entry.kind === 'project' ? '' : 'hover:bg-accent/20 cursor-pointer'
                  }`}
                  onClick={entry.kind === 'project' ? undefined : () => onEntryClick(entry)}
                  title={entry.kind === 'project' ? undefined : '打开对话标签'}
                >
                  <td className="px-3 py-1.5">
                    <button
                      className="text-muted-foreground hover:text-foreground mr-1 inline-flex h-4 w-4 items-center justify-center"
                      onClick={(e) => {
                        e.stopPropagation()
                        onToggle(entry.key)
                      }}
                      tabIndex={-1}
                    >
                      <ChevronRight
                        className={`h-3 w-3 transition-transform ${expandedKeys.has(entry.key) ? 'rotate-90' : ''}`}
                      />
                    </button>
                    <span
                      className={`rounded px-1 py-0 text-[10px] font-medium ${
                        entry.kind === 'task'
                          ? 'bg-status-warning/15 text-status-warning'
                          : entry.kind === 'project'
                            ? 'bg-status-success/15 text-status-success'
                            : 'bg-primary/10 text-primary/80'
                      }`}
                    >
                      {entry.kind === 'task' ? '任务' : entry.kind === 'project' ? '项目' : '会话'}
                    </span>
                  </td>
                  <td className="max-w-[160px] truncate px-3 py-1.5 text-xs">
                    {entry.name}
                    {!entry.sessionTitle && entry.kind !== 'project' && (
                      <span className="ml-1 rounded bg-muted px-1 py-0 text-[9px] text-muted-foreground">
                        无归属
                      </span>
                    )}
                  </td>
                  <td className="hidden px-3 py-1.5 text-xs text-muted-foreground sm:table-cell">
                    {entry.sessionTitle || '--'}
                  </td>
                  <td className="hidden px-3 py-1.5 text-xs text-muted-foreground lg:table-cell">
                    {entry.agentName || '--'}
                  </td>
                  <td className="px-3 py-1.5">
                    {status ? (
                      <span
                        className={`flex items-center gap-1 text-xs ${status.color}`}
                        title={`运行状态：${status.label}${entry.taskStatus ? ` · 任务原始状态：${entry.taskStatus}` : ''}${entry.stateStatus ? ` · state 真值：${entry.stateStatus}` : ''}`}
                      >
                        {status.icon}
                        {status.label}
                        {entry.taskStatus && (
                          <span
                            className="bg-muted text-muted-foreground rounded px-1 py-0 text-[10px]"
                            title={`任务状态：${taskStatusLabel(entry.taskStatus)}（原始值 ${entry.taskStatus}）`}
                          >
                            任务:{taskStatusLabel(entry.taskStatus)}
                          </span>
                        )}
                      </span>
                    ) : (
                      <span className="text-xs text-muted-foreground">—</span>
                    )}
                  </td>
                  <td className="hidden px-3 py-1.5 text-xs tabular-nums text-muted-foreground md:table-cell">
                    {entry.kind === 'project' ? '—' : formatDuration(durationMs)}
                  </td>
                  <td className="px-3 py-1.5 text-right text-xs tabular-nums text-muted-foreground">
                    {tokenTotal != null ? tokenTotal.toLocaleString() : '--'}
                  </td>
                  <td className="px-3 py-1.5">
                    <div className="flex items-center gap-0.5">
                      {entry.kind !== 'project' && (
                        <button
                          className="bg-primary/15 text-primary hover:bg-primary/25 flex h-6 w-6 items-center justify-center rounded"
                          onClick={(e) => {
                            e.stopPropagation()
                            onEntryClick(entry)
                          }}
                          title="打开对话"
                          aria-label="打开对话"
                          tabIndex={-1}
                        >
                          <MessageSquare className="h-3.5 w-3.5" />
                        </button>
                      )}
                      {entry.kind === 'project' && entry.projectId && (
                        <button
                          className="bg-primary/15 text-primary hover:bg-primary/25 flex h-6 w-6 items-center justify-center rounded"
                          onClick={(e) => {
                            e.stopPropagation()
                            onAction(entry, 'open-folder')
                          }}
                          title="在系统文件管理器中打开项目文件夹"
                          aria-label="打开文件夹"
                          tabIndex={-1}
                        >
                          <FolderOpen className="h-3.5 w-3.5" />
                        </button>
                      )}
                      {entry.kind === 'project' && entry.projectId && (
                        <button
                          className="text-muted-foreground hover:bg-status-error/15 hover:text-status-error flex h-6 w-6 items-center justify-center rounded"
                          onClick={(e) => {
                            e.stopPropagation()
                            onAction(entry, 'delete')
                          }}
                          title="删除项目"
                          aria-label="删除项目"
                          tabIndex={-1}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </button>
                      )}
                      <button
                        className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-6 w-6 items-center justify-center rounded"
                        onClick={(e) => {
                          e.stopPropagation()
                          onAction(entry, 'copy')
                        }}
                        title="复制管道 ID"
                        aria-label="复制管道 ID"
                        tabIndex={-1}
                      >
                        <CopyIcon className="h-3.5 w-3.5" />
                      </button>
                      {entry.kind === 'task' && entry.taskId && entry.status === 'running' && (
                        <>
                          <button
                            className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-6 w-6 items-center justify-center rounded"
                            onClick={(e) => {
                              e.stopPropagation()
                              onAction(entry, 'pause')
                            }}
                            title="暂停任务"
                            tabIndex={-1}
                          >
                            <PauseCircle className="h-3.5 w-3.5" />
                          </button>
                          <button
                            className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-6 w-6 items-center justify-center rounded"
                            onClick={(e) => {
                              e.stopPropagation()
                              onAction(entry, 'cancel')
                            }}
                            title="取消任务"
                            tabIndex={-1}
                          >
                            <XCircle className="h-3.5 w-3.5" />
                          </button>
                        </>
                      )}
                      {entry.kind === 'task' && entry.taskId && entry.status === 'suspended' && (
                        <button
                          className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-6 w-6 items-center justify-center rounded"
                          onClick={(e) => {
                            e.stopPropagation()
                            onAction(entry, 'resume')
                          }}
                          title="恢复任务"
                          tabIndex={-1}
                        >
                          <PlayCircle className="h-3.5 w-3.5" />
                        </button>
                      )}
                      {entry.workspacePath && (
                        <button
                          className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-6 w-6 items-center justify-center rounded"
                          onClick={(e) => {
                            e.stopPropagation()
                            onAction(entry, 'workspace')
                          }}
                          title={`打开工作空间: ${entry.workspacePath}`}
                          tabIndex={-1}
                        >
                          <FolderOpen className="h-3.5 w-3.5" />
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
                {expandedKeys.has(entry.key) && (
                  <tr className="border-b">
                    <td colSpan={8} className="bg-muted/20 px-3 py-1.5">
                      <EntryDetail entry={entry} depth={0} />
                    </td>
                  </tr>
                )}
              </React.Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
