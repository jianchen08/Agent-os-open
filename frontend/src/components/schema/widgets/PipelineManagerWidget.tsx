/** 任务管理面板 Widget（统一管道管理）
 *
 * 职责（与其他插件 widget 同构：拿数据 → 填充展示）：
 * - 管道数据：内核 `GET /api/v1/pipelines/runs` 快照（pipelineRegistryStore）
 *   + WS 流式/任务事件实时增量 + 任务列表（longTermTaskStore 权威）
 * - 展示：执行中/最近完成两组，组内按归属会话分组（会话 → 管道 包含关系），
 *   无归属（孤儿）管道单独平铺；树视图/列表视图切换；树子级点击展开/收起
 *   （默认收起），条目详情走独立「详细信息」按钮、与树展开解耦（树操作不关
 *   详情，详情停留显示）；操作按钮（打开对话/暂停/恢复/取消/复制 ID/打开工作空间）
 * - 任务与执行管道一对一绑定（2026-08-24 裁定）：条目行即任务行，不包
 *   任务节点层——子任务/子管道直挂该条目行下，只一个层级
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  ChevronRight,
  CircleDot,
  CheckCircle2,
  Loader2,
  XCircle,
  PauseCircle,
  PlayCircle,
  ClipboardList,
  FolderTree,
  CopyIcon,
  ExternalLink,
  InfoIcon,
  MessageSquare,
} from '@/assets/icons'
import { useAllTasksQuery } from '@/hooks/queries/useAllTasksQuery'
import { invalidateLongTermTasks } from '@/hooks/queries/useLongTermTasksQuery'
import { usePipelineRunsQuery, usePipelineStatesQuery } from '@/hooks/queries/usePipelineRunsQuery'
import { useSessionsQuery, readSessions, ensureSessionsLoaded } from '@/hooks/queries/useSessionsQuery'
import { pauseTask, resumeTask, cancelTask } from '@/services/api/tasks'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import type { PipelineStatus, PipelineViewEntry } from '@/types/pipeline'
import type { AgentTab } from '@/types/task'

// ═════════════════════════════════════════════════════════════════
// 辅助
// ═════════════════════════════════════════════════════════════════

/** 任务状态 → 管道运行状态映射（对齐 RunStatus 语义） */
function taskStatusToPipelineStatus(taskStatus: string | undefined): PipelineStatus | null {
  switch (taskStatus) {
    case 'pending':
    case 'running':
    case 'evaluating':
    case 'planning':
      return 'running'
    case 'stopped':
    case 'suspended':
    case 'blocked':
    case 'paused':
      return 'suspended'
    case 'completed':
      return 'completed'
    case 'failed':
    case 'timeout':
    case 'cancelled':
      return 'failed'
    default:
      return null
  }
}

/** 从任务对象提取管道 ID（后端字段名与前端类型并存时双取） */
function taskPipelineId(task: Record<string, unknown>): string | undefined {
  const raw = task.pipeline_run_id ?? task.pipelineRunId
  if (typeof raw === 'string' && raw) return raw
  const meta = task.metadata as Record<string, unknown> | undefined
  const metaRaw = meta?.pipeline_run_id ?? meta?.pipelineRunId
  return typeof metaRaw === 'string' && metaRaw ? metaRaw : undefined
}

/** 格式化耗时（ms → "3m 20s" / "1h 2m" / "45s"） */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || isNaN(ms) || ms < 0) return '--'
  const totalSec = Math.floor(ms / 1000)
  if (totalSec < 60) return `${totalSec}s`
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`
  const h = Math.floor(m / 60)
  return h > 0 ? `${h}h ${m % 60}m` : `${m}m`
}

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

/** 管道状态 → 展示文案 */
const PIPELINE_STATUS_LABELS: Record<string, string> = {
  running: '运行中',
  suspended: '已暂停',
  completed: '已完成',
  failed: '失败',
}

/** 状态 → 图标 + 颜色 */
function statusIcon(status: string): { icon: React.ReactNode; color: string; label: string } {
  const map: Record<string, { icon: React.ReactNode; color: string }> = {
    running: { icon: <Loader2 className="h-4 w-4 animate-spin" />, color: 'text-status-info' },
    suspended: { icon: <PauseCircle className="h-4 w-4" />, color: 'text-status-pending' },
    completed: { icon: <CheckCircle2 className="h-4 w-4" />, color: 'text-status-success' },
    failed: { icon: <XCircle className="h-4 w-4" />, color: 'text-status-error' },
  }
  const conf = map[status] ?? { icon: <CircleDot className="h-4 w-4" />, color: 'text-status-pending' }
  return { icon: conf.icon, color: conf.color, label: PIPELINE_STATUS_LABELS[status] ?? status }
}

// ═════════════════════════════════════════════════════════════════
// 主组件
// ═════════════════════════════════════════════════════════════════

export function PipelineManagerWidget(_rawProps: Record<string, unknown>) {
  /** 管道 runs 快照（query 化：queryKeys.pipelineRuns，30s 兜底轮询 + WS 流式事件增量） */
  const { data: registryRuns = {} } = usePipelineRunsQuery()
  /** 管道 state 摘要（query 化：queryKeys.pipelineStates；states 侧失败仅 runs 快照仍可用） */
  const { data: registryStates = {}, error: registryStatesError } = usePipelineStatesQuery()
  /** 实时 token 用量（cost_update 事件驱动） */
  const usageByPipeline = useContextUsageStore((s) => s.usageByPipeline)
  /** 全量任务（task_service 插件端点，不过滤 long-term；任务节点/任务管道判定权威源）。
   *  query 化：queryKeys.pipelineAllTasks，30s 轮询 + 窗口聚焦刷新替代原本地 interval */
  const { data: allTasks = [], error: tasksError } = useAllTasksQuery()
  /** 会话列表（按 thread_id 取会话标题） */
  const { data: sessions = [] } = useSessionsQuery()

  /** 管道条目派生：注册表快照 + 任务列表（含管道 ID 的任务）合并，按开始时间倒序 */
  const pipelineEntries: PipelineViewEntry[] = useMemo(() => {
    // 任务 → 管道 ID 映射（判定任务类型 + 取任务名/进度；全量任务）
    const taskByPipeline = new Map<string, Record<string, unknown>>()
    for (const task of allTasks) {
      const pid = taskPipelineId(task)
      if (pid) taskByPipeline.set(pid, task)
    }
    const sessionById = new Map(sessions.map((s) => [s.id, s]))

    const entries: PipelineViewEntry[] = []
    const seen = new Set<string>()

    // 1) 注册表运行快照（会话管道 + 已落 runs 的任务管道）
    const runsSorted = Object.values(registryRuns).sort((a, b) =>
      b.started_at.localeCompare(a.started_at),
    )
    for (const run of runsSorted) {
      const key = run.pipeline_id || run.run_id
      seen.add(key)
      const task = run.pipeline_id ? taskByPipeline.get(run.pipeline_id) : undefined
      const session = run.thread_id ? sessionById.get(run.thread_id) : undefined
      const live = run.pipeline_id ? usageByPipeline[run.pipeline_id] : undefined
      // 内核 state 摘要（phase/迭代真值；runs 覆盖的管道也可能有 state 补充）
      const st = run.pipeline_id ? registryStates[run.pipeline_id] : undefined
      const kind = task ? 'task' : 'session'
      const name =
        (task ? String(task.title ?? '') : '')
        || (session ? session.title : '')
        || (run.thread_id ? `会话 ${run.thread_id.slice(0, 8)}` : '')
        || (run.pipeline_id ? run.pipeline_id.slice(0, 12) : run.run_id.slice(0, 12))
      entries.push({
        key,
        pipelineId: run.pipeline_id,
        runId: run.run_id,
        threadId: run.thread_id,
        status: run.status,
        startedAt: run.started_at,
        endedAt: run.ended_at,
        kind,
        name,
        agentName: task ? String(task.agentName ?? task.agent_name ?? '') || undefined : undefined,
        taskId: task ? String(task.id) : undefined,
        // 归属会话标题（threadId 未命中会话列表 = 无归属孤儿管道）
        sessionTitle: session ? session.title : undefined,
        // 任务工作空间路径（供"打开工作空间"；取 metadata.ws_meta.path / workspace）
        workspacePath: task
          ? String(
              (task.metadata as { ws_meta?: { path?: string } } | undefined)?.ws_meta?.path
              || task.workspace
              || '',
            ) || undefined
          : undefined,
        progress:
          typeof (task?.progress as Record<string, unknown> | undefined)?.progressPercent === 'number'
            ? Number((task?.progress as Record<string, unknown>).progressPercent)
            : undefined,
        totalTokens: run.total_tokens ?? null,
        currentPhase: st?.state.current_phase,
        messageCount: st?.state.message_count,
        // 原始状态与 state 真值（不被 4 态映射吞掉——evaluating/planning 等细态可见）
        taskStatus: task ? String(task.status ?? '') || undefined : undefined,
        stateStatus: st
          ? String(st.state['task.status'] ?? st.state.status ?? '') || undefined
          : undefined,
        stateEnded: st?.state.ended === true ? true : undefined,
        rawError: st?.state.raw_error ?? undefined,
        liveUsage: live
          ? {
              promptTokens: live.promptTokens,
              completionTokens: live.completionTokens,
              totalTokens: live.totalTokens,
            }
          : undefined,
      })
    }

    // 2) 任务派生条目：有管道 ID 但未进 runs 的任务（旧引擎占位 run / 未落槽窗口）。
    //    落 seen——否则下方 states 独立条目循环会为同一管道再建一行（同 key 双行，
    //    展开/详情状态互相串扰）
    for (const [pid, task] of taskByPipeline) {
      if (seen.has(pid)) continue
      seen.add(pid)
      const taskStatus = String(task.status ?? '')
      // 未知状态不再丢弃（2026-08-19）：保留条目，4 态映射兜底 running，原始状态在行内展示
      const mapped = taskStatusToPipelineStatus(taskStatus) ?? 'running'
      const st = registryStates[pid]
      const timestamps = task.timestamps as Record<string, unknown> | undefined
      const startedAt =
        (timestamps?.startedAt as string | undefined)
        || (timestamps?.createdAt as string | undefined)
        || new Date().toISOString()
      const completedAt = timestamps?.completedAt as string | undefined
      entries.push({
        key: pid,
        pipelineId: pid,
        runId: pid,
        threadId: String(task.threadId ?? task.thread_id ?? '') || undefined,
        status: mapped,
        startedAt,
        endedAt: completedAt,
        kind: 'task',
        name: String(task.title ?? pid.slice(0, 12)),
        agentName: String(task.agentName ?? task.agent_name ?? '') || undefined,
        taskId: String(task.id),
        sessionTitle: undefined,
        workspacePath: String(
          (task.metadata as { ws_meta?: { path?: string } } | undefined)?.ws_meta?.path
          || task.workspace
          || '',
        ) || undefined,
        progress:
          typeof (task.progress as Record<string, unknown> | undefined)?.progressPercent === 'number'
            ? Number((task.progress as Record<string, unknown>).progressPercent)
            : undefined,
        totalTokens: null,
        taskStatus: taskStatus || undefined,
        currentPhase: st?.state.current_phase,
        messageCount: st?.state.message_count,
        stateStatus: st
          ? String(st.state['task.status'] ?? st.state.status ?? '') || undefined
          : undefined,
        stateEnded: st?.state.ended === true ? true : undefined,
        rawError: st?.state.raw_error ?? undefined,
      })
    }

    // 3) 内核 state 独有条目：registry runs 未覆盖的管道（如重启后仅存在于
    //    checkpoint 的历史管道）——直接从 state 摘要生成节点（会话/阶段/迭代真值）
    for (const st of Object.values(registryStates)) {
      if (seen.has(st.pipeline_id)) continue
      seen.add(st.pipeline_id)
      const s = st.state
      const session = st.thread_id ? sessionById.get(st.thread_id) : undefined
      // ended=true → completed；status=active 但未 ended → running
      const mapped: PipelineStatus = s.raw_error ? 'failed' : s.ended ? 'completed' : 'running'
      const meta = s.metadata as Record<string, unknown> | undefined
      const executionCtx = meta?.execution_context as Record<string, unknown> | undefined
      entries.push({
        key: st.pipeline_id,
        pipelineId: st.pipeline_id,
        runId: st.pipeline_id,
        threadId: st.thread_id,
        status: mapped,
        startedAt: new Date(0).toISOString(),
        kind: 'session',
        name:
          (session ? session.title : '')
          || s.display_name
          || s.name
          || (st.thread_id ? `会话 ${st.thread_id.slice(0, 8)}` : st.pipeline_id.slice(0, 12)),
        sessionTitle: session ? session.title : undefined,
        currentPhase: s.current_phase,
        messageCount: s.message_count,
        stateStatus: String(s['task.status'] ?? s.status ?? '') || undefined,
        stateEnded: s.ended === true ? true : undefined,
        rawError: s.raw_error ?? undefined,
        workspacePath: executionCtx
          ? String(
              (executionCtx.workspace as Record<string, unknown> | undefined)?.source_path
              || '',
            ) || undefined
          : undefined,
        totalTokens: null,
      })
    }

    return entries
  }, [registryRuns, registryStates, usageByPipeline, allTasks, sessions])

  /** 展示视图：tree（树视图）/ list（列表视图） */
  const [viewMode, setViewMode] = useState<'tree' | 'list'>('tree')
  /** 类型筛选：all / task / session */
  const [kindFilter, setKindFilter] = useState<'all' | 'task' | 'session'>('all')
  /** 状态筛选（空串 = 全部；默认全部——任务管理面板打开即见所有管道状态） */
  const [statusFilter, setStatusFilter] = useState('')
  /** 树子级展开 key 集合（点击展开/收起，默认收起——树是点击展开） */
  const [treeOpenKeys, setTreeOpenKeys] = useState<Set<string>>(new Set())
  /** 展开详情的条目 key 集合（与树展开解耦：树收起不关详情，信息停留显示） */
  const [detailOpenKeys, setDetailOpenKeys] = useState<Set<string>>(new Set())
  /** 秒级 ticker（执行中条目耗时实时刷新） */
  const [nowMs, setNowMs] = useState(() => Date.now())
  useEffect(() => {
    const hasActive = pipelineEntries.some(
      (e) => e.status === 'running' || e.status === 'suspended',
    )
    if (!hasActive) return
    const timer = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [pipelineEntries])

  /** 按类型 + 状态筛选管道条目 */
  const filteredPipelineEntries = useMemo(
    () =>
      pipelineEntries.filter((entry) => {
        if (kindFilter !== 'all' && entry.kind !== kindFilter) return false
        if (statusFilter) {
          const statusMatch =
            entry.status === statusFilter
            || (statusFilter === 'running' && entry.status === 'running')
            || (statusFilter === 'completed' && entry.status === 'completed')
            || (statusFilter === 'failed' && entry.status === 'failed')
            || (statusFilter === 'paused' && entry.status === 'suspended')
          if (!statusMatch) return false
        }
        return true
      }),
    [pipelineEntries, kindFilter, statusFilter],
  )

  /** 管道树：会话主管道顶层 → 任务条目行（一对一绑定：条目行即任务行，
   *  2026-08-24 裁定不再包任务节点层）→ 子任务/子管道直挂该条目行下；
   *  无任务归属的管道直接挂主管道下；孤儿（无会话归属）顶层平铺。
   *  状态分组按顶层节点状态划分，子树跟随父级不拆散层级。 */
  const pipelineTree = useMemo(() => {
    const entryByKey = new Map(filteredPipelineEntries.map((e) => [e.key, e]))
    // 任务索引（id → task；全量任务）
    const taskById = new Map(allTasks.map((t) => [String(t.id ?? ''), t]))
    // 任务父子映射：taskId → parentTaskId（父容器任务 = metadata.parent_project_id，
    // 容器任务 = task.owned 声明、非管道——子任务据此挂到父任务条目行下）
    const parentTaskOf = new Map<string, string>()
    for (const t of allTasks) {
      const pid = String(t.parent_task_id ?? t.parentTaskId ?? '')
      if (pid) parentTaskOf.set(String(t.id), pid)
      const meta = t.metadata as Record<string, unknown> | undefined
      const proj = String(meta?.parent_project_id ?? '')
      if (proj) parentTaskOf.set(String(t.id), proj)
    }
    // 会话主管道：threadId 组内 session.pipelineIds[0]（缺省取最早 started_at）
    const threadTop = new Map<string, string>()
    const threadGroups = new Map<string, PipelineViewEntry[]>()
    for (const e of filteredPipelineEntries) {
      if (!e.threadId) continue
      const list = threadGroups.get(e.threadId) ?? []
      list.push(e)
      threadGroups.set(e.threadId, list)
    }
    for (const [tid, list] of threadGroups) {
      const session = sessions.find((s) => s.id === tid)
      const mainPid = session?.pipelineIds?.[0]
      const main = mainPid ? list.find((e) => e.pipelineId === mainPid) : undefined
      const top =
        main ?? [...list].sort((a, b) => a.startedAt.localeCompare(b.startedAt))[0]
      threadTop.set(tid, top.key)
    }
    const childrenMap = new Map<string, PipelineTreeNode[]>()
    // 节点注册表（key → 节点；childrenMap 归属合并的目标查找）
    const nodeByKey = new Map<string, PipelineTreeNode>()
    const roots: PipelineTreeNode[] = []
    const pushChild = (parentKey: string, node: PipelineTreeNode) => {
      const list = childrenMap.get(parentKey) ?? []
      list.push(node)
      childrenMap.set(parentKey, list)
    }
    // 任务 id → 其条目行 key（一对一绑定的条目行即任务行；子任务据此直挂）
    const taskEntryKeyOf = new Map<string, string>()
    // 任务条目行（归属在循环 3 统一判定：父任务行 → 父管道条目 → 会话主管道 → 顶层）
    const taskRowNodes = new Map<string, PipelineTreeNode>()
    // 1) 任务管道条目：直接成行（不包任务节点层——一对一绑定一个层级）
    for (const e of filteredPipelineEntries) {
      if (e.kind === 'task' && e.taskId) {
        const node: PipelineTreeNode = { key: e.key, entry: e, depth: 0, children: [] }
        nodeByKey.set(e.key, node)
        if (!taskEntryKeyOf.has(e.taskId)) taskEntryKeyOf.set(e.taskId, e.key)
        taskRowNodes.set(e.taskId, node)
        continue
      }
      // 2) 非任务管道（会话主管道/直接子管道/孤儿）
      let parentKey: string | undefined
      if (
        e.threadId
        && threadTop.has(e.threadId)
        && threadTop.get(e.threadId) !== e.key
      ) {
        parentKey = threadTop.get(e.threadId)
      }
      const node: PipelineTreeNode = { key: e.key, entry: e, depth: 0, children: [] }
      nodeByKey.set(e.key, node)
      if (parentKey && (entryByKey.has(parentKey) || childrenMap.has(parentKey))) {
        pushChild(parentKey, node)
      } else {
        roots.push(node)
      }
    }
    // 3) 任务条目行归属：父任务条目行（parent_task_id=父任务 id）→ 父管道条目
    //    （子任务管道出生即 lineage.parent_pipeline_id = 提交者管道 id）→ 会话
    //    主管道 → 顶层
    for (const [taskId, node] of taskRowNodes) {
      const parentTaskId = parentTaskOf.get(taskId)
      if (parentTaskId) {
        const parentEntryKey = taskEntryKeyOf.get(parentTaskId)
        if (
          parentEntryKey
          && (entryByKey.has(parentEntryKey) || childrenMap.has(parentEntryKey))
        ) {
          pushChild(parentEntryKey, node)
          continue
        }
        if (entryByKey.has(parentTaskId) || childrenMap.has(parentTaskId)) {
          pushChild(parentTaskId, node)
          continue
        }
      }
      const t = taskById.get(taskId)
      const tmeta = t?.metadata as Record<string, unknown> | undefined
      // thread_id 在 TaskModel.metadata 中（API 顶层无此字段）
      const tid = String(t?.thread_id ?? t?.threadId ?? tmeta?.thread_id ?? tmeta?.threadId ?? '')
      const topKey = tid && threadTop.has(tid) ? threadTop.get(tid)! : ''
      if (topKey && (entryByKey.has(topKey) || childrenMap.has(topKey))) {
        pushChild(topKey, node)
      } else {
        roots.push(node)
      }
    }
    // childrenMap 并入渲染树（pushChild 仅登记，未并入则子层级整体丢失）
    for (const [parentKey, nodes] of childrenMap) {
      const parent = nodeByKey.get(parentKey)
      if (parent) parent.children.push(...nodes)
      else roots.push(...nodes)
    }
    const sortByStart = (a: PipelineTreeNode, b: PipelineTreeNode) =>
      String(b.entry.startedAt).localeCompare(String(a.entry.startedAt))
    const build = (list: PipelineTreeNode[], depth: number): PipelineTreeNode[] =>
      [...list].sort(sortByStart).map((n) => ({
        ...n,
        depth,
        children: build(n.children, depth + 1),
      }))
    return build(roots, 0)
  }, [filteredPipelineEntries, allTasks, sessions])

  /** 展开/折叠树子级（行首 chevron / 任务节点行点击） */
  const toggleTreeNode = useCallback((key: string) => {
    setTreeOpenKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  /** 展开/收起详情（独立于树展开——树操作不会动这份状态） */
  const toggleDetail = useCallback((key: string) => {
    setDetailOpenKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }, [])

  /** 打开对话（统一跳转逻辑）：先确认管道归属会话（会话列表未加载时先拉取，
   *  避免误判无归属而在当前会话新建标签），有归属 → 定位/跳转到对应会话标签
   *  （主管道=主标签、子管道=已有子标签跳转/无则新建），无归属 → 创建独立标签。 */
  const handleEntryClick = useCallback(async (entry: PipelineViewEntry) => {
    const pipelineId = entry.pipelineId || entry.key
    // 会话列表未加载时先拉取（防止把有归属管道误判为孤儿而在当前会话建标签）
    if (readSessions().length === 0) {
      await ensureSessionsLoaded().catch(() => {})
    }
    const hasSession =
      !!entry.threadId
      && readSessions().some((s) => s.id === entry.threadId)
    if (hasSession && entry.threadId) {
      const ok = await navigateToPipeline(pipelineId, {
        agentName: entry.name,
        agentLevel: 2,
        taskId: entry.taskId,
        status: entry.status,
      }).catch((e) => {
        console.error('[PipelineManager] 管道导航失败', e)
        return false
      })
      if (!ok) {
        // navigateToPipeline 在无活跃会话等场景会拒绝——直接切换到管道所属会话
        // （setActiveSession 会激活主管道标签并加载消息，等价于"打开对应会话标签"）
        await useSessionListStore
          .getState()
          .setActiveSession(entry.threadId)
          .catch((e) => {
            console.error('[PipelineManager] 切换会话失败', e)
            useNotificationStore.getState().addNotification({
              title: '无法打开对话',
              message: `管道 ${pipelineId.slice(0, 12)} 无归属会话或会话不存在`,
              priority: 'normal',
              category: 'alert',
              isBlocking: false,
              autoDismissMs: 5000,
            })
          })
      }
      return
    }
    // 无会话归属：不定位，直接跳转打开对应标签页
    const tabStore = useAgentTabStore.getState()
    const existingTab = tabStore.tabs.find((t) => t.pipelineRunId === pipelineId)
    if (existingTab) {
      tabStore.switchToTab(existingTab.id)
      return
    }
    const tabId = `sub-${pipelineId}`
    tabStore.openSubAgentTab({
      agentId: entry.taskId || pipelineId,
      agentName: entry.name,
      parentRecordId: pipelineId,
      agentLevel: 2,
      taskId: entry.taskId,
      status: (entry.status ?? 'running') as AgentTab['status'],
      setActive: true,
      pipelineId,
    })
    tabStore.loadTabMessages(tabId, pipelineId)
  }, [sessions])

  /** 打开工作空间文件树标签（0.1 任务树节点同款：workspace://<taskId> 数据源，
   *  标签内 FiveSpaceLayout 另有"打开文件夹"按钮调后端 explorer.exe 打开目录） */
  const openWorkspaceTab = useCallback((taskId: string, title: string) => {
    const layoutStore = useLayoutModeStore.getState()
    const tabId = `ws-tree-${taskId}`
    const existingTab = layoutStore.workspaceTabs.find((t) => t.id === tabId)
    if (existingTab) {
      layoutStore.setActiveTab(tabId)
      return
    }
    layoutStore.addWorkspaceTab({
      id: tabId,
      title: title || '工作空间',
      icon: '📁',
      moduleId: '__dynamic__',
      component: 'file_tree',
      dataSource: `workspace://${taskId}`,
      isActive: true,
      isPinned: false,
    })
  }, [])

  /** 操作：暂停/恢复/取消（任务管道）/复制 ID/打开工作空间 */
  const handleAction = useCallback(
    async (
      entry: PipelineViewEntry,
      action: 'pause' | 'resume' | 'cancel' | 'copy' | 'workspace',
    ) => {
      const pipelineId = entry.pipelineId || entry.key
      if (action === 'copy') {
        try {
          await navigator.clipboard.writeText(pipelineId)
        } catch {
          // clipboard 不可用时静默失败
        }
        return
      }
      if (action === 'workspace') {
        if (!entry.taskId) return
        openWorkspaceTab(entry.taskId, entry.name)
        return
      }
      if (!entry.taskId) return
      try {
        if (action === 'pause') {
          await pauseTask(entry.taskId)
        } else if (action === 'resume') {
          await resumeTask(entry.taskId)
        } else {
          await cancelTask(entry.taskId)
        }
        // 操作后刷新任务列表（任务派生条目随之更新）——query 化：invalidate 由
        // 活跃 useLongTermTasksQuery 订阅自动重拉
        invalidateLongTermTasks()
      } catch (e) {
        console.error('[PipelineManager] 管道操作失败', action, e)
      }
    },
    [openWorkspaceTab],
  )

  /** 状态筛选选项（与任务状态词表对齐） */
  const STATUS_OPTIONS = [
    { value: '', label: '全部' },
    { value: 'running', label: '运行中' },
    { value: 'paused', label: '已暂停' },
    { value: 'completed', label: '已完成' },
    { value: 'failed', label: '失败' },
  ]

  return (
    <div className="flex h-full flex-col">
      {/* 工具栏：视图切换 + 类型筛选 + 状态筛选 */}
      <div className="border-b px-3 py-2">
        <div className="flex flex-wrap items-center gap-1">
          <button
            onClick={() => setViewMode('tree')}
            className={`rounded-md px-2 py-0.5 text-[11px] transition-colors ${
              viewMode === 'tree'
                ? 'bg-primary/15 text-primary font-medium'
                : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'
            }`}
            title="树视图（会话分组）"
          >
            <FolderTree className="mr-1 inline h-3 w-3" />
            树
          </button>
          <button
            onClick={() => setViewMode('list')}
            className={`rounded-md px-2 py-0.5 text-[11px] transition-colors ${
              viewMode === 'list'
                ? 'bg-primary/15 text-primary font-medium'
                : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'
            }`}
            title="列表视图"
          >
            <ClipboardList className="mr-1 inline h-3 w-3" />
            列表
          </button>
          <span className="bg-border mx-1 h-3 w-px" />
          <button
            onClick={() => setKindFilter('all')}
            className={`rounded-md px-2 py-0.5 text-[11px] transition-colors ${
              kindFilter === 'all'
                ? 'bg-primary/15 text-primary font-medium'
                : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'
            }`}
          >
            全部
          </button>
          <button
            onClick={() => setKindFilter('task')}
            className={`rounded-md px-2 py-0.5 text-[11px] transition-colors ${
              kindFilter === 'task'
                ? 'bg-primary/15 text-primary font-medium'
                : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'
            }`}
          >
            任务
          </button>
          <button
            onClick={() => setKindFilter('session')}
            className={`rounded-md px-2 py-0.5 text-[11px] transition-colors ${
              kindFilter === 'session'
                ? 'bg-primary/15 text-primary font-medium'
                : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'
            }`}
          >
            会话
          </button>
          <span className="bg-border mx-1 h-3 w-px" />
          {STATUS_OPTIONS.map((opt) => (
            <button
              key={opt.value}
              onClick={() => setStatusFilter(opt.value)}
              className={`rounded-md px-2 py-0.5 text-[11px] transition-colors ${
                statusFilter === opt.value
                  ? 'bg-primary/15 text-primary font-medium'
                  : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground'
              }`}
            >
              {opt.label}
            </button>
          ))}
        </div>
      </div>

      {/* 降级留痕（FE6/FE9）：states 或任务列表拉取失败时提示，不伪装成"无数据" */}
      {(registryStatesError || tasksError) && (
        <div className="border-b bg-status-warning/10 px-3 py-1.5 text-[11px] text-status-warning">
          {registryStatesError && (
            <div>管道状态拉取失败（{registryStatesError.message}）——phase/任务状态可能不全，30s 后自动重试。</div>
          )}
          {tasksError && (
            <div>任务列表不可用（{tasksError.message}）——管道列表仍可展示，30s 后自动重试。</div>
          )}
        </div>
      )}

      {/* 管道管理视图 */}
      <div className="min-h-0 flex-1 overflow-auto">
        {viewMode === 'list' ? (
          <PipelineTable
            entries={filteredPipelineEntries}
            nowMs={nowMs}
            expandedKeys={detailOpenKeys}
            onToggle={toggleDetail}
            onEntryClick={handleEntryClick}
            onAction={handleAction}
          />
        ) : (
          <PipelineTree
            tree={pipelineTree}
            nowMs={nowMs}
            treeOpenKeys={treeOpenKeys}
            detailOpenKeys={detailOpenKeys}
            onTreeToggle={toggleTreeNode}
            onToggleDetail={toggleDetail}
            onEntryClick={handleEntryClick}
            onAction={handleAction}
          />
        )}
      </div>

    </div>
  )
}

// ═════════════════════════════════════════════════════════════════
// 树视图：执行中/最近完成两组，组内按会话分组，孤儿平铺
// ═════════════════════════════════════════════════════════════════

/** 管道树节点：管道条目（任务一对一绑定时条目行即任务行，子级直挂其下） */
interface PipelineTreeNode {
  /** 节点 key（条目 key） */
  key: string
  /** 管道条目 */
  entry: PipelineViewEntry
  depth: number
  children: PipelineTreeNode[]
}

function PipelineTree({
  tree,
  nowMs,
  treeOpenKeys,
  detailOpenKeys,
  onTreeToggle,
  onToggleDetail,
  onEntryClick,
  onAction,
}: {
  tree: PipelineTreeNode[]
  nowMs: number
  treeOpenKeys: Set<string>
  detailOpenKeys: Set<string>
  onTreeToggle: (key: string) => void
  onToggleDetail: (key: string) => void
  onEntryClick: (entry: PipelineViewEntry) => void
  onAction: (entry: PipelineViewEntry, action: 'pause' | 'resume' | 'cancel' | 'copy' | 'workspace') => void
}) {
  // 主管道（顶层）与容器任务按状态分组：执行中 / 最近完成（子树跟随父级）
  const isActiveNode = (n: PipelineTreeNode): boolean =>
    n.entry.status === 'running' || n.entry.status === 'suspended'
  const split = (nodes: PipelineTreeNode[]) => {
    const active: PipelineTreeNode[] = []
    const done: PipelineTreeNode[] = []
    for (const n of nodes) {
      if (isActiveNode(n)) active.push(n)
      else done.push(n)
    }
    return { active, done }
  }
  const { active, done } = split(tree)
  const activeCount = countNodes(active)
  const doneCount = countNodes(done)
  return (
    <div className="py-1">
      {tree.length === 0 && (
        <div className="text-muted-foreground px-4 py-6 text-center text-xs">
          暂无管道运行记录
        </div>
      )}
      {active.length > 0 && (
        <TreeGroup
          title="执行中的管道"
          count={activeCount}
          nodes={active}
          nowMs={nowMs}
          treeOpenKeys={treeOpenKeys}
          detailOpenKeys={detailOpenKeys}
          onTreeToggle={onTreeToggle}
          onToggleDetail={onToggleDetail}
          onEntryClick={onEntryClick}
          onAction={onAction}
        />
      )}
      {done.length > 0 && (
        <TreeGroup
          title="最近完成"
          count={doneCount}
          nodes={done}
          nowMs={nowMs}
          treeOpenKeys={treeOpenKeys}
          detailOpenKeys={detailOpenKeys}
          onTreeToggle={onTreeToggle}
          onToggleDetail={onToggleDetail}
          onEntryClick={onEntryClick}
          onAction={onAction}
        />
      )}
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
  onTreeToggle,
  onToggleDetail,
  onEntryClick,
  onAction,
}: {
  title: string
  count: number
  nodes: PipelineTreeNode[]
  nowMs: number
  treeOpenKeys: Set<string>
  detailOpenKeys: Set<string>
  onTreeToggle: (key: string) => void
  onToggleDetail: (key: string) => void
  onEntryClick: (entry: PipelineViewEntry) => void
  onAction: (entry: PipelineViewEntry, action: 'pause' | 'resume' | 'cancel' | 'copy' | 'workspace') => void
}) {
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
                onTreeToggle={onTreeToggle}
                onToggleDetail={onToggleDetail}
                onEntryClick={onEntryClick}
                onAction={onAction}
                orphan={!node.entry.threadId}
              />
              {node.children.length > 0 && treeOpenKeys.has(node.key) && (
                <TreeChildren
                  nodes={node.children}
                  nowMs={nowMs}
                  treeOpenKeys={treeOpenKeys}
                  detailOpenKeys={detailOpenKeys}
                  onTreeToggle={onTreeToggle}
                  onToggleDetail={onToggleDetail}
                  onEntryClick={onEntryClick}
                  onAction={onAction}
                />
              )}
            </div>
          ))}
        </div>
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
  onTreeToggle,
  onToggleDetail,
  onEntryClick,
  onAction,
}: {
  nodes: PipelineTreeNode[]
  nowMs: number
  treeOpenKeys: Set<string>
  detailOpenKeys: Set<string>
  onTreeToggle: (key: string) => void
  onToggleDetail: (key: string) => void
  onEntryClick: (entry: PipelineViewEntry) => void
  onAction: (entry: PipelineViewEntry, action: 'pause' | 'resume' | 'cancel' | 'copy' | 'workspace') => void
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
            onTreeToggle={onTreeToggle}
            onToggleDetail={onToggleDetail}
            onEntryClick={onEntryClick}
            onAction={onAction}
            orphan={!node.entry.threadId}
          />
          {node.children.length > 0 && treeOpenKeys.has(node.key) && (
            <TreeChildren
              nodes={node.children}
              nowMs={nowMs}
              treeOpenKeys={treeOpenKeys}
              detailOpenKeys={detailOpenKeys}
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
  onTreeToggle: (key: string) => void
  onToggleDetail: (key: string) => void
  onEntryClick: (entry: PipelineViewEntry) => void
  onAction: (entry: PipelineViewEntry, action: 'pause' | 'resume' | 'cancel' | 'copy' | 'workspace') => void
}) {
  const status = statusIcon(entry.status)
  const durationMs = entry.endedAt
    ? new Date(entry.endedAt).getTime() - new Date(entry.startedAt).getTime()
    : nowMs - new Date(entry.startedAt).getTime()
  const tokenTotal = entryTokenTotal(entry)

  return (
    <div>
      <div
        className="hover:bg-accent group flex cursor-pointer items-center gap-1.5 py-1.5 pr-2 transition-colors"
        style={{ paddingLeft: `${depth * 16 + 8}px` }}
        onClick={() => onEntryClick(entry)}
        title="打开对话标签"
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
              : 'bg-primary/10 text-primary/80'
          }`}
        >
          {entry.kind === 'task' ? '任务' : '会话'}
        </span>
        {orphan && (
          <span className="shrink-0 rounded bg-muted px-1 py-0 text-[10px] text-muted-foreground">
            无归属
          </span>
        )}
        {/* 状态图标（title 带原始状态——4 态映射的细态不丢） */}
        <span
          className="shrink-0"
          title={`运行状态：${status.label}${entry.taskStatus ? ` · 任务原始状态：${entry.taskStatus}` : ''}${entry.stateStatus ? ` · state 真值：${entry.stateStatus}` : ''}`}
        >
          {status.icon}
        </span>
        {/* 名称 */}
        <span className="text-foreground/90 min-w-0 flex-1 truncate text-sm">{entry.name}</span>
        {/* 原始状态（与 4 态标签不同才显示：evaluating/planning 等细态） */}
        {entry.taskStatus && status.label !== entry.taskStatus && (
          <span
            className="text-muted-foreground/70 hidden shrink-0 text-[10px] font-mono md:inline"
            title="任务原始状态"
          >
            {entry.taskStatus}
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
        {/* agent */}
        {entry.agentName && (
          <span className="bg-primary/10 text-primary/70 hidden shrink-0 rounded px-1.5 py-0 text-[10px] sm:inline">
            {entry.agentName}
          </span>
        )}
        {/* 状态标签 */}
        <span className={`shrink-0 rounded px-1 text-[10px] font-medium ${status.color}`}>
          {status.label}
        </span>
        {/* 耗时 */}
        <span className="text-muted-foreground/60 hidden shrink-0 text-[10px] tabular-nums md:inline">
          {formatDuration(durationMs)}
        </span>
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
          {entry.kind === 'task' && entry.workspacePath && (
            <button
              className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-6 w-6 items-center justify-center rounded transition-colors"
              onClick={(e) => {
                e.stopPropagation()
                onAction(entry, 'workspace')
              }}
              title={`打开工作空间: ${entry.workspacePath}`}
              tabIndex={-1}
            >
              <ExternalLink className="h-3.5 w-3.5" />
            </button>
          )}
        </div>
      </div>
      {detailOpen && <EntryDetail entry={entry} depth={depth} />}
    </div>
  )
}

/** 展开详情：管道 ID/归属/时间/token 明细/进度 */
function EntryDetail({ entry, depth }: { entry: PipelineViewEntry; depth: number }) {
  const tokenTotal = entryTokenTotal(entry)
  const rows: Array<[string, string]> = [
    ['管道 ID', entry.pipelineId || entry.key],
    ['运行 ID', entry.runId],
    ['归属', entry.sessionTitle ? `${entry.sessionTitle}（${entry.threadId}）` : '无归属'],
    ['开始', entry.startedAt],
  ]
  if (entry.endedAt) rows.push(['结束', entry.endedAt])
  if (entry.agentName) rows.push(['Agent', entry.agentName])
  // state 真值关键数据（2026-08-19：任务条目上的关键信息不缺位）
  if (entry.taskStatus) rows.push(['任务状态', entry.taskStatus])
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

function PipelineTable({
  entries,
  nowMs,
  expandedKeys,
  onToggle,
  onEntryClick,
  onAction,
}: {
  entries: PipelineViewEntry[]
  nowMs: number
  expandedKeys: Set<string>
  onToggle: (key: string) => void
  onEntryClick: (entry: PipelineViewEntry) => void
  onAction: (entry: PipelineViewEntry, action: 'pause' | 'resume' | 'cancel' | 'copy' | 'workspace') => void
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
            const status = statusIcon(entry.status)
            const durationMs = entry.endedAt
              ? new Date(entry.endedAt).getTime() - new Date(entry.startedAt).getTime()
              : nowMs - new Date(entry.startedAt).getTime()
            const tokenTotal = entryTokenTotal(entry)
            return (
              <React.Fragment key={entry.key}>
                <tr
                  className="hover:bg-accent/20 cursor-pointer border-b last:border-b-0"
                  onClick={() => onEntryClick(entry)}
                  title="打开对话标签"
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
                          : 'bg-primary/10 text-primary/80'
                      }`}
                    >
                      {entry.kind === 'task' ? '任务' : '会话'}
                    </span>
                  </td>
                  <td className="max-w-[160px] truncate px-3 py-1.5 text-xs">
                    {entry.name}
                    {!entry.sessionTitle && (
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
                    <span
                      className={`flex items-center gap-1 text-xs ${status.color}`}
                      title={`运行状态：${status.label}${entry.taskStatus ? ` · 任务原始状态：${entry.taskStatus}` : ''}${entry.stateStatus ? ` · state 真值：${entry.stateStatus}` : ''}`}
                    >
                      {status.icon}
                      {status.label}
                      {entry.taskStatus && status.label !== entry.taskStatus && (
                        <span className="text-muted-foreground/70 text-[10px] font-mono">
                          {entry.taskStatus}
                        </span>
                      )}
                    </span>
                  </td>
                  <td className="hidden px-3 py-1.5 text-xs tabular-nums text-muted-foreground md:table-cell">
                    {formatDuration(durationMs)}
                  </td>
                  <td className="px-3 py-1.5 text-right text-xs tabular-nums text-muted-foreground">
                    {tokenTotal != null ? tokenTotal.toLocaleString() : '--'}
                  </td>
                  <td className="px-3 py-1.5">
                    <div className="flex items-center gap-0.5">
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
                      {entry.kind === 'task' && entry.workspacePath && (
                        <button
                          className="text-muted-foreground hover:bg-accent hover:text-foreground flex h-6 w-6 items-center justify-center rounded"
                          onClick={(e) => {
                            e.stopPropagation()
                            onAction(entry, 'workspace')
                          }}
                          title="打开工作空间"
                          tabIndex={-1}
                        >
                          <ExternalLink className="h-3.5 w-3.5" />
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
