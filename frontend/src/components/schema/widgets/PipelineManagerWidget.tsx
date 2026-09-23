/** 任务管理面板 Widget（统一管道管理）
 *
 * 职责（与其他插件 widget 同构：拿数据 → 填充展示）：
 * - 管道数据：内核 `GET /api/v1/pipelines/runs` 快照（pipelineRegistryStore）
 *   + WS 流式/任务事件实时增量 + 任务列表（longTermTaskStore 权威）
 * - 展示：执行中/最近完成两组，组内按归属会话分组（会话 → 管道 包含关系），
 *   无归属（孤儿）管道单独平铺；树视图/列表视图切换；树子级点击展开/收起
 *   （默认收起），条目详情走独立「详细信息」按钮、与树展开解耦（树操作不关
 *   详情，详情停留显示）；操作按钮（打开对话/暂停/恢复/取消/复制 ID/打开工作空间）
 * - 任务与执行管道一对一绑定：条目行即任务行，不包
 *   任务节点层——子任务/子管道直挂该条目行下，只一个层级
 */

import { useQueryClient } from '@tanstack/react-query'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Loader2, ClipboardList, FolderTree, Trash2 } from '@/assets/icons'
import { Button } from '@/components/ui/button'
import { pipelineStatusToTabStatus, TERMINAL_PIPELINE_STATUSES } from './pipelineStatusVisuals'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useAllTasksQuery } from '@/hooks/queries/useAllTasksQuery'
import { invalidateLongTermTasks } from '@/hooks/queries/useLongTermTasksQuery'
import { usePipelineRunsQuery, usePipelineStatesQuery } from '@/hooks/queries/usePipelineRunsQuery'
import { useProjectsQuery } from '@/hooks/queries/useProjectsQuery'
import { useSessionsQuery, readSessions, ensureSessionsLoaded } from '@/hooks/queries/useSessionsQuery'
import { useAsyncResource } from '@/hooks/useAsyncResource'
import { useElementVisible } from '@/hooks/useElementVisible'
import { useVisibleRefetch } from '@/hooks/useVisibleRefetch'
import { queryKeys } from '@/services/query/queryKeys'
import apiClient from '@/services/api/client'
import { WORKSPACE_SERVICE_ENDPOINTS } from '@/services/api/endpoints.generated'
import { mapStateInfoToViewModel, type PipelineStateEntryViewModel } from '@/services/api/pipelines'
import { deleteProject, pauseTask, resumeTask, cancelTask } from '@/services/api/tasks'
import { navigateToPipeline } from '@/services/pipelineNavigator'
import { useAgentTabStore } from '@/stores/agentTabStore'
import { useContextUsageStore } from '@/stores/contextUsageStore'
import { openWorkspaceTreeTab } from './workspaceTreeTab'
import {
  PipelineTable,
  PipelineTree,
  type PipelineTreeNode,
} from './PipelineManagerTreeParts'
import { useNotificationStore } from '@/stores/notificationStore'
import { useSessionListStore } from '@/stores/sessionListStore'
import { taskStatusToPipelineStatus } from '@/types/taskStatus'
import type { PipelineViewEntry } from '@/types/pipeline'

// ═════════════════════════════════════════════════════════════════
// 辅助
// ═════════════════════════════════════════════════════════════════


/** 从任务对象提取管道 ID（后端字段名与前端类型并存时双取） */
function taskPipelineId(task: Record<string, unknown>): string | undefined {
  const raw = task.pipeline_run_id ?? task.pipelineRunId
  if (typeof raw === 'string' && raw) return raw
  const meta = task.metadata as Record<string, unknown> | undefined
  const metaRaw = meta?.pipeline_run_id ?? meta?.pipelineRunId
  return typeof metaRaw === 'string' && metaRaw ? metaRaw : undefined
}

/** 任务定位高亮时长（focusTaskId 定位行的高亮窗口，超时自动清除） */
const LOCATE_HIGHLIGHT_MS = 2500

/** 在树中查找节点 key 的祖先路径（父→子序、含自身；找不到返回空数组） */
function findTreePathKeys(nodes: PipelineTreeNode[], key: string, trail: string[] = []): string[] {
  for (const node of nodes) {
    if (node.key === key) return [...trail, node.key]
    const deep = findTreePathKeys(node.children, key, [...trail, node.key])
    if (deep.length > 0) return deep
  }
  return []
}


// ═════════════════════════════════════════════════════════════════
// 主组件
// ═════════════════════════════════════════════════════════════════

export function PipelineManagerWidget({ focusTaskId }: { focusTaskId?: string }) {
  // 离屏暂停：面板根节点不可见（工作区非激活 tab display:none / 窗口最小化）时
  // 冻结全部 30s 轮询与秒级耗时 ticker（隐藏面板的采集/重渲染纯负载），恢复
  // 可见立即 invalidate 对账一次，不等下一周期
  const { ref: rootRef, visible: panelVisible } = useElementVisible<HTMLDivElement>()
  /** 管道 runs 快照（query 化：queryKeys.pipelineRuns，30s 兜底轮询 + WS 流式事件增量） */
  const runsQuery = usePipelineRunsQuery(panelVisible ? undefined : false)
  const registryRuns = runsQuery.data ?? {}
  /** 管道 state 摘要（query 化：queryKeys.pipelineStates；states 侧失败仅 runs 快照仍可用） */
  const statesQuery = usePipelineStatesQuery(panelVisible ? undefined : false)
  const registryStates = statesQuery.data ?? {}
  const registryStatesError = statesQuery.error
  /** 实时 token 用量（cost_update 事件驱动） */
  const usageByPipeline = useContextUsageStore((s) => s.usageByPipeline)
  /** 全量任务（task_service 插件端点，不过滤 long-term；任务节点/任务管道判定权威源）。
   *  query 化：queryKeys.pipelineAllTasks，30s 轮询 + 窗口聚焦刷新替代原本地 interval */
  const tasksQuery = useAllTasksQuery(panelVisible ? undefined : false)
  const allTasks = tasksQuery.data ?? []
  const tasksError = tasksQuery.error
  const queryClient = useQueryClient()
  useVisibleRefetch(
    panelVisible,
    [queryKeys.pipelineRuns, queryKeys.pipelineStates, queryKeys.pipelineAllTasks],
    queryClient,
  )
  /** 会话列表（按 thread_id 取会话标题） */
  const { data: sessions = [] } = useSessionsQuery()
  // 项目登记行（project = 文件夹+登记：树的项目分组节点数据源）。
  // 四态统一（OBS-R258-1）：副面降级决策在此声明——登记拉取失败不阻断任务
  // 列表（登记行只是分组/归局面），降级为「项目分组不可用」留痕横幅 + 重试，
  // 不伪装成「无项目」空态
  const projectsQuery = useProjectsQuery()
  const projectsResource = useAsyncResource(projectsQuery, {
    fallbackErrorText: '项目登记获取失败',
  })
  const projects = useMemo(() => projectsResource.data?.items ?? [], [projectsResource.data])
  /** 主列表首载中（任一主源 pending 且整体尚无条目 → 显示加载而非「暂无」空态） */
  const listLoading =
    runsQuery.isLoading || statesQuery.isLoading || tasksQuery.isLoading || projectsResource.isLoading

  /** 管道条目派生：注册表快照 + 任务列表（含管道 ID 的任务）合并，按开始时间倒序 */
  const pipelineEntries: PipelineViewEntry[] = useMemo(() => {
    // state 摘要 → 视图模型（扁平键名/三源状态推断收敛在适配层，组件只消费视图）
    const stateViews: Record<string, PipelineStateEntryViewModel> = {}
    for (const st of Object.values(registryStates)) {
      stateViews[st.pipeline_id] = mapStateInfoToViewModel(st)
    }
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
      // 内核 state 摘要视图（phase/迭代真值；runs 覆盖的管道也可能有 state 补充）
      const st = run.pipeline_id ? stateViews[run.pipeline_id] : undefined
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
        // 血缘根会话（自环子任务管道的 thread_id=自身 id 不在会话列表，
        // 真实归属用户会话由血缘键承载）
        originSessionId: st?.originSessionId,
        status: run.status,
        startedAt: run.started_at,
        endedAt: run.ended_at,
        kind,
        name,
        agentName: task ? String(task.agentName ?? task.agent_name ?? '') || undefined : undefined,
        taskId: task ? String(task.id) : undefined,
        // 归属会话标题（threadId 未命中会话列表 = 无归属孤儿管道）
        sessionTitle: session ? session.title : undefined,
        // 任务工作空间路径（R3：state 真值优先 ws_meta.path/workspace，任务
        // metadata 镜像回退；非任务条目也走 state 通道）
        workspacePath:
          st?.workspacePath
          ?? (task
            ? String(
                (task.metadata as { ws_meta?: { path?: string } } | undefined)?.ws_meta?.path
                || task.workspace
                || '',
              ) || undefined
            : undefined),
        progress:
          typeof (task?.progress as Record<string, unknown> | undefined)?.progressPercent === 'number'
            ? Number((task?.progress as Record<string, unknown>).progressPercent)
            : undefined,
        totalTokens: run.total_tokens ?? null,
        currentPhase: st?.currentPhase,
        messageCount: st?.messageCount,
        // 原始状态与 state 真值（不被 4 态映射吞掉——evaluating/planning 等细态可见）
        taskStatus: task ? String(task.status ?? '') || undefined : undefined,
        stateStatus: st?.taskStatus,
        mode: st?.mode,
        stateEnded: st?.ended,
        rawError: st?.rawError,
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
      // 未知状态不再丢弃也不再猜 running：保留条目落 'unknown' 视图态，
      // 原始状态在行内展示（词表归一与告警见 types/taskStatus）
      const mapped = taskStatusToPipelineStatus(taskStatus)
      const st = stateViews[pid]
      // BUG-2b 终态防御（ADR 2026-09-14）：state 视图已持 runs 权威终态
      // （冷行 run_status overlay / reap 投影）的行，崩溃遗留的陈旧
      // task.status 投影不再参与状态归组——终态优先，不再显示为幽灵执行中。
      // BUG-21 无证据防御：runs/state 双面均无此管道行（未起跑任务管道无
      // message_slots/checkpoint，state 冷兜底不出口）时，任务域未决态
      // （pending/running/evaluating → running）没有运行态真值可依——runs
      // 权威无行 = 无运行证据，落 'unknown' 不再猜 running；任务域终态
      // （completed/failed/cancelled 等自有证据）照常投影。
      const status =
        st && TERMINAL_PIPELINE_STATUSES.has(st.status)
          ? st.status
          : !st && mapped === 'running'
            ? 'unknown'
            : mapped
      // 时间真值链：任务端点顶层只有 created_at/updated_at（state 聚合行
      // 当前恒为空串）；无真值落空串 = 耗时列按缺失态 '--' 展示。禁止以
      // 渲染时刻兜底起点——BUG-21 全面板假 0ms 即该兜底所致（now 起点
      // 减 now ≈ 0）。结束时刻仅终态任务取（updated_at ≈ 终态写入时刻）。
      const startedAt =
        (typeof task.created_at === 'string' && task.created_at) ||
        (typeof task.createdAt === 'string' && task.createdAt) ||
        ''
      const updatedAt =
        (typeof task.updated_at === 'string' && task.updated_at) ||
        (typeof task.updatedAt === 'string' && task.updatedAt) ||
        ''
      entries.push({
        key: pid,
        pipelineId: pid,
        runId: pid,
        threadId: String(task.threadId ?? task.thread_id ?? '') || undefined,
        originSessionId: st?.originSessionId,
        status,
        startedAt,
        endedAt:
          updatedAt && TERMINAL_PIPELINE_STATUSES.has(status) ? updatedAt : undefined,
        kind: 'task',
        name: String(task.title ?? pid.slice(0, 12)),
        agentName: String(task.agentName ?? task.agent_name ?? '') || undefined,
        taskId: String(task.id),
        sessionTitle: undefined,
        workspacePath:
          st?.workspacePath
          ?? (String(
            (task.metadata as { ws_meta?: { path?: string } } | undefined)?.ws_meta?.path
            || task.workspace
            || '',
          ) || undefined),
        progress:
          typeof (task.progress as Record<string, unknown> | undefined)?.progressPercent === 'number'
            ? Number((task.progress as Record<string, unknown>).progressPercent)
            : undefined,
        totalTokens: null,
        taskStatus: taskStatus || undefined,
        currentPhase: st?.currentPhase,
        messageCount: st?.messageCount,
        stateStatus: st?.taskStatus,
        mode: st?.mode,
        stateEnded: st?.ended,
        rawError: st?.rawError,
      })
    }

    // 3) 内核 state 独有条目：registry runs 未覆盖的管道（如重启后仅存在于
    //    checkpoint 的历史管道）——直接从 state 视图生成节点（会话/阶段/迭代真值；
    //    归一状态由适配层单一出口给出，旧 checkpoint 数据回退推断不失真）
    for (const st of Object.values(stateViews)) {
      if (seen.has(st.pipelineId)) continue
      seen.add(st.pipelineId)
      const session = st.threadId ? sessionById.get(st.threadId) : undefined
      entries.push({
        key: st.pipelineId,
        pipelineId: st.pipelineId,
        runId: st.pipelineId,
        threadId: st.threadId,
        originSessionId: st.originSessionId,
        status: st.status,
        // state 独有条目无启动时间真值：置空串（未知），耗时列按 '--' 展示。
        // 禁止发明 epoch-0 占位——endedAt - epoch0 会算出 56 年级假耗时
        // （GUI 黑盒测试 2026-09-11：stress2-10 已完成却显示 496969h 9m）。
        startedAt: '',
        kind: 'session',
        name:
          (session ? session.title : '')
          || st.displayName
          || st.name
          || (st.threadId ? `会话 ${st.threadId.slice(0, 8)}` : st.pipelineId.slice(0, 12)),
        sessionTitle: session ? session.title : undefined,
        currentPhase: st.currentPhase,
        messageCount: st.messageCount,
        stateStatus: st.taskStatus,
        mode: st.mode,
        stateEnded: st.ended,
        rawError: st.rawError,
        // 工作区坐标（state 真值：ws_meta.path/workspace；替代旧
        // metadata.execution_context.source_path 推断）
        workspacePath: st.workspacePath,
        totalTokens: null,
      })
    }

    // 项目登记行（项目=文件夹的登记数据行；列表视图与树视图同款展示，
    // 树视图的分组节点仍由 pipelineTree 合成块建——两侧 key 同为 project-<id>）。
    // 登记行不是管道运行：无运行态（status 缺省，不进执行中分组/状态筛选/
    // 耗时 ticker），startedAt 语义=登记时间（详情卡按「登记时间」展示）。
    for (const p of projects) {
      const pid = String(p.id)
      entries.push({
        key: `project-${pid}`,
        runId: `project-${pid}`,
        projectId: pid,
        startedAt: String(p.timestamps?.createdAt ?? ''),
        kind: 'project',
        name: String(p.goal ?? p.id),
        workspacePath:
          (p.metadata as { path?: string } | undefined)?.path || undefined,
      })
    }

    return entries
  }, [registryRuns, registryStates, usageByPipeline, allTasks, sessions, projects])

  /** 展示视图：tree（树视图）/ list（列表视图） */
  const [viewMode, setViewMode] = useState<'tree' | 'list'>('tree')
  /** 类型筛选：all / task / session */
  const [kindFilter, setKindFilter] = useState<'all' | 'task' | 'session'>('all')
  /** 状态筛选（缺省 running——面板默认只看运行中的管道/任务，用户裁定
   *  2026-09-21；空串 = 全部） */
  const [statusFilter, setStatusFilter] = useState('running')
  /** 树子级展开 key 集合（点击展开/收起，默认收起——树是点击展开） */
  const [treeOpenKeys, setTreeOpenKeys] = useState<Set<string>>(new Set())
  /** 展开详情的条目 key 集合（与树展开解耦：树收起不关详情，信息停留显示） */
  const [detailOpenKeys, setDetailOpenKeys] = useState<Set<string>>(new Set())
  /** 秒级 ticker（执行中条目耗时实时刷新）；面板不可见时冻结（整面板重渲染纯负载） */
  const [nowMs, setNowMs] = useState(() => Date.now())
  useEffect(() => {
    if (!panelVisible) return
    const hasActive = pipelineEntries.some(
      (e) => e.status === 'running' || e.status === 'suspended',
    )
    if (!hasActive) return
    const timer = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [pipelineEntries, panelVisible])

  /** 项目删除确认弹窗（待删项目；null = 关闭） */
  const [projectDeleteTarget, setProjectDeleteTarget] = useState<{
    id: string
    name: string
  } | null>(null)
  /** 删除口径：连子任务一起删除（缺省仅挂起保留）/ 连文件夹一起删除 */
  const [deleteChildren, setDeleteChildren] = useState(false)
  const [deleteFiles, setDeleteFiles] = useState(false)
  const [deletingProject, setDeletingProject] = useState(false)

  /** 确认删除项目：成功后刷新项目登记与任务列表（级联口径下子任务随之消失） */
  const handleProjectDeleteConfirm = useCallback(async () => {
    if (!projectDeleteTarget) return
    setDeletingProject(true)
    try {
      const result = await deleteProject(projectDeleteTarget.id, { deleteChildren, deleteFiles })
      queryClient.invalidateQueries({ queryKey: queryKeys.projectsPrefix })
      invalidateLongTermTasks()
      useNotificationStore.getState().addNotification({
        title: '项目已删除',
        message:
          result.deleted_children > 0
            ? `项目「${projectDeleteTarget.name}」已删除，连同 ${result.deleted_children} 个子任务`
            : `项目「${projectDeleteTarget.name}」已删除，名下子任务已挂起保留`,
        priority: 'normal',
        category: 'alert',
        isBlocking: false,
        autoDismissMs: 6000,
        sourceLabel: '前端',
      })
      setProjectDeleteTarget(null)
    } catch (e) {
      useNotificationStore.getState().addNotification({
        title: '删除项目失败',
        message: (e as { message?: string })?.message || '删除失败，请稍后重试',
        priority: 'normal',
        category: 'alert',
        isBlocking: false,
        autoDismissMs: 6000,
        sourceLabel: '前端',
      })
    } finally {
      setDeletingProject(false)
    }
  }, [projectDeleteTarget, deleteChildren, deleteFiles, queryClient])

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
   *  不再包任务节点层）→ 子任务/子管道直挂该条目行下；
   *  无任务归属的管道直接挂主管道下；孤儿（无会话归属）顶层平铺。
   *  状态分组按顶层节点状态划分，子树跟随父级不拆散层级。 */
  const pipelineTree = useMemo(() => {
    const entryByKey = new Map(filteredPipelineEntries.map((e) => [e.key, e]))
    // 任务索引（id → task；全量任务）
    const taskById = new Map(allTasks.map((t) => [String(t.id ?? ''), t]))
    // 任务父子映射：taskId → parentTaskId（任务链父 = parent_task_id）；
    // 项目挂靠 = metadata.parent_project_id（登记 id）——树里挂项目分组节点下
    const parentTaskOf = new Map<string, string>()
    const projectOfTask = new Map<string, string>()
    for (const t of allTasks) {
      const pid = String(t.parent_task_id ?? t.parentTaskId ?? '')
      if (pid) parentTaskOf.set(String(t.id), pid)
      const meta = t.metadata as Record<string, unknown> | undefined
      const proj = String(meta?.parent_project_id ?? '')
      if (proj) projectOfTask.set(String(t.id), proj)
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
    // 项目分组节点（登记行合成；无任务挂靠的项目也显示为空分组）
    const projectNodeKeys = new Map<string, string>() // projectId → node key
    for (const p of projects) {
      const pid = String(p.id)
      const key = `project-${pid}`
      projectNodeKeys.set(pid, key)
      const node: PipelineTreeNode = {
        key,
        entry: {
          key,
          runId: key,
          projectId: pid,
          startedAt: String(p.timestamps?.createdAt ?? ''),
          kind: 'project',
          name: String(p.goal ?? p.id),
          workspacePath:
            (p.metadata as { path?: string } | undefined)?.path || undefined,
        },
        depth: 0,
        children: [],
      }
      nodeByKey.set(key, node)
      roots.push(node)
    }
    // 任务 id → 其条目行 key（一对一绑定的条目行即任务行；子任务据此直挂）
    const taskEntryKeyOf = new Map<string, string>()
    // 任务条目行（归属在循环 3 统一判定：父任务行 → 父管道条目 → 会话主管道 → 顶层）
    const taskRowNodes = new Map<string, PipelineTreeNode>()
    // 1) 任务管道条目：直接成行（不包任务节点层——一对一绑定一个层级）
    for (const e of filteredPipelineEntries) {
      // 项目登记行节点由上方合成块统一建（任务挂靠目标 + 根），跳过防同 key 重复行
      if (e.kind === 'project') continue
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
    //    主管道 → 顶层。自挂守卫（与循环 2 的 threadTop!==key 同款）：父任务/
    //    线程顶层解析到自身（会话主管道缺席时 threadTop 回退最早任务条目=自身）
    //    时落根平铺——树与列表同源同量，建树环节不得丢条目
    for (const [taskId, node] of taskRowNodes) {
      // 项目挂靠优先：登记命中的任务挂项目分组节点（项目不是管道，不能当 lineage 父）
      const projId = projectOfTask.get(taskId)
      if (projId && projectNodeKeys.has(projId)) {
        pushChild(projectNodeKeys.get(projId)!, node)
        continue
      }
      const parentTaskId = parentTaskOf.get(taskId)
      if (parentTaskId && parentTaskId !== taskId) {
        const parentEntryKey = taskEntryKeyOf.get(parentTaskId)
        if (
          parentEntryKey
          && parentEntryKey !== node.key
          && (entryByKey.has(parentEntryKey) || childrenMap.has(parentEntryKey))
        ) {
          pushChild(parentEntryKey, node)
          continue
        }
        if (parentTaskId !== node.key && (entryByKey.has(parentTaskId) || childrenMap.has(parentTaskId))) {
          pushChild(parentTaskId, node)
          continue
        }
      }
      const t = taskById.get(taskId)
      const tmeta = t?.metadata as Record<string, unknown> | undefined
      // thread_id 在 TaskModel.metadata 中（API 顶层无此字段）
      const tid = String(t?.thread_id ?? t?.threadId ?? tmeta?.thread_id ?? tmeta?.threadId ?? '')
      const topKey = tid && threadTop.has(tid) ? threadTop.get(tid)! : ''
      if (topKey && topKey !== node.key && (entryByKey.has(topKey) || childrenMap.has(topKey))) {
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
    // 状态筛选生效时空项目分组不占位（缺省「运行中」视图：没有在跑任务的
    // 项目分组隐藏，面板只剩真正在跑的内容；「全部」视图保留空分组——登记
    // 行可见性不受筛选排挤）
    const visibleRoots = statusFilter
      ? roots.filter((n) => n.entry.kind !== 'project' || n.children.length > 0)
      : roots
    const sortByStart = (a: PipelineTreeNode, b: PipelineTreeNode) =>
      String(b.entry.startedAt).localeCompare(String(a.entry.startedAt))
    const build = (list: PipelineTreeNode[], depth: number): PipelineTreeNode[] =>
      [...list].sort(sortByStart).map((n) => ({
        ...n,
        depth,
        children: build(n.children, depth + 1),
      }))
    return build(visibleRoots, 0)
  }, [filteredPipelineEntries, allTasks, sessions, projects, statusFilter])

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

  /**
   * 通知点击定位（OBS-R259-1）：focusTaskId 非空时定位该任务行——解除状态
   * 筛选收窄（面板默认只看运行中，终态任务行必须可见）、展开树祖先链、行内
   * 滚动定位并短暂高亮。任务行不在条目集（列表未含/已清理）→ 只保留开签，
   * 不定位（筛选与树状态不动）。
   */
  const [locatedKey, setLocatedKey] = useState<string | null>(null)
  const locatedFocusRef = useRef('')
  useEffect(() => {
    if (!focusTaskId || locatedFocusRef.current === focusTaskId) return
    const entry = pipelineEntries.find((e) => e.kind === 'task' && e.taskId === focusTaskId)
    if (!entry) return
    locatedFocusRef.current = focusTaskId
    setKindFilter('all')
    setStatusFilter('')
    setLocatedKey(entry.key)
  }, [focusTaskId, pipelineEntries])

  // 树展开与滚动跟随定位目标：筛选解除后 pipelineTree 才含目标行（首帧筛选
  // 收窄时树可能为空），故本效应在其就绪后补祖先链展开，渲染后滚动至行内，
  // 短暂高亮后自动清除
  useEffect(() => {
    if (!locatedKey) return
    const path = findTreePathKeys(pipelineTree, locatedKey)
    path.pop()
    if (path.length > 0) {
      // 无新增时返回原 Set（同引用 bail-out）：定位效应随 pipelineTree 引用
      // 变化重跑，若恒产新 Set 会构成每渲染一写的渲染循环
      setTreeOpenKeys((prev) => {
        const missing = path.filter((k) => !prev.has(k))
        if (missing.length === 0) return prev
        const next = new Set(prev)
        for (const k of missing) next.add(k)
        return next
      })
    }
    const raf = requestAnimationFrame(() => {
      for (const el of document.querySelectorAll('[data-entry-key]')) {
        if (el.getAttribute('data-entry-key') === locatedKey) {
          if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center' })
          break
        }
      }
    })
    const timer = window.setTimeout(() => setLocatedKey(null), LOCATE_HIGHLIGHT_MS)
    return () => {
      cancelAnimationFrame(raf)
      window.clearTimeout(timer)
    }
  }, [locatedKey, pipelineTree])

  /** 打开对话（统一跳转逻辑）：先确认管道归属会话（会话列表未加载时先拉取，
   *  避免误判无归属而在当前会话新建标签），有归属 → 定位/跳转到对应会话标签
   *  （主管道=主标签、子管道=已有子标签跳转/无则新建），无归属 → 创建独立标签。 */
  const handleEntryClick = useCallback(async (entry: PipelineViewEntry) => {
    // 项目 = 文件夹 + 登记行，无管道无会话（ADR 2026-08-27）——没有可打开的对话
    if (entry.kind === 'project') return
    const pipelineId = entry.pipelineId || entry.key
    // 会话列表未加载时先拉取；拉取失败即阻断跳转——继续执行会把有归属管道
    // 误判为孤儿而在当前会话新建重复标签（L540 注释要避免的正是这个后果）。
    if (readSessions().length === 0) {
      try {
        await ensureSessionsLoaded()
      } catch (e) {
        console.error('[PipelineManager] 会话列表拉取失败，中止定位', e)
        useNotificationStore.getState().addNotification({
          title: '无法定位对话',
          message: `会话列表加载失败，请稍后重试（管道 ${pipelineId.slice(0, 12)}）`,
          priority: 'normal',
          category: 'alert',
          isBlocking: false,
          autoDismissMs: 5000,
          sourceLabel: '前端',
        })
        return
      }
    }
    // 归属会话解析：threadId 命中会话列表为第一真值。自环子任务管道
    // （thread_id=自身 id）不在任何会话成员列表里——runs 读面联结出的
    // thread_id 不会命中会话列表，真实归属会话经血缘 origin_session_id
    // 兜底（出生写面真值，指向根用户会话）。
    // 待办（解耦方案 P2-6）：内核 runs/state 响应直接携带归属会话字段后，
    // 删除本兜底分支。
    const sessionsNow = readSessions()
    let owningSessionId: string | undefined
    let ownsViaLineage = false
    if (entry.threadId && sessionsNow.some((s) => s.id === entry.threadId)) {
      owningSessionId = entry.threadId
    } else if (
      entry.originSessionId
      && sessionsNow.some((s) => s.id === entry.originSessionId)
    ) {
      owningSessionId = entry.originSessionId
      ownsViaLineage = true
    }
    if (owningSessionId) {
      const ok = await navigateToPipeline(pipelineId, {
        agentName: entry.name,
        agentLevel: 2,
        taskId: entry.taskId,
        status: entry.status,
        // 自环子任务管道在任何会话 pipelineIds 里都查不到——血缘会话作定位
        // 提示传入，导航器切到归属会话开子标签（不在当前会话落孤儿标签）
        fallbackSessionId: ownsViaLineage ? owningSessionId : undefined,
      }).catch((e) => {
        // 记日志并返回 false：导航失败走下方 setActiveSession 归属会话兜底链
        // （OBS-R258-1 吞错误规则登记）
        console.error('[PipelineManager] 管道导航失败', e)
        return false
      })
      if (!ok) {
        // navigateToPipeline 在无活跃会话等场景会拒绝——直接切换到管道所属会话
        // （setActiveSession 会激活主管道标签并加载消息，等价于"打开对应会话标签"）
        await useSessionListStore
          .getState()
          .setActiveSession(owningSessionId)
          .catch((e) => {
            console.error('[PipelineManager] 切换会话失败', e)
            useNotificationStore.getState().addNotification({
              title: '无法打开对话',
              message: `管道 ${pipelineId.slice(0, 12)} 无归属会话或会话不存在`,
              priority: 'normal',
              category: 'alert',
              isBlocking: false,
              autoDismissMs: 5000,
              sourceLabel: '前端',
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
      status: pipelineStatusToTabStatus(entry.status ?? 'unknown'),
      setActive: true,
      pipelineId,
    })
    tabStore.loadTabMessages(tabId, pipelineId)
  }, [sessions])

  /** 打开工作空间文件树标签（共享入口见 workspaceTreeTab.ts） */
  const openWorkspaceTab = useCallback((taskId: string, title: string) => {
    openWorkspaceTreeTab(taskId, title)
  }, [])

  /** 操作：暂停/恢复/取消（任务管道）/复制 ID/打开工作空间/打开项目文件夹/删除项目 */
  const handleAction = useCallback(
    async (
      entry: PipelineViewEntry,
      action: 'pause' | 'resume' | 'cancel' | 'copy' | 'workspace' | 'open-folder' | 'delete',
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
      if (action === 'delete') {
        // 项目删除：打开确认弹窗（口径选择在弹窗内），不在行上直接删
        if (!entry.projectId) return
        setDeleteChildren(false)
        setDeleteFiles(false)
        setProjectDeleteTarget({ id: entry.projectId, name: entry.name })
        return
      }
      if (action === 'open-folder') {
        // 项目文件夹打开：workspaces open 端点的项目登记通道按 id 解析文件夹，
        // 有 IDE 连接器走连接器、否则系统文件管理器（与工作区标签内按钮同链路）
        if (!entry.projectId) return
        try {
          const resp = await apiClient.post(
            WORKSPACE_SERVICE_ENDPOINTS.workspaces_open.replace(
              '{container_task_id}',
              entry.projectId,
            ),
          )
          const data = resp?.data as { success?: boolean; message?: string } | undefined
          if (data && data.success === false) {
            useNotificationStore.getState().addNotification({
              title: '打开文件夹失败',
              message: data.message || '后端未能打开项目文件夹',
              priority: 'normal',
              category: 'alert',
              isBlocking: false,
              autoDismissMs: 6000,
              sourceLabel: '前端',
            })
          }
        } catch (e) {
          console.error('[PipelineManager] 打开项目文件夹失败', e)
          useNotificationStore.getState().addNotification({
            title: '打开文件夹失败',
            message: `项目 ${entry.name} 打开失败，请稍后重试`,
            priority: 'normal',
            category: 'alert',
            isBlocking: false,
            autoDismissMs: 6000,
            sourceLabel: '前端',
          })
        }
        return
      }
      if (action === 'workspace') {
        // R3：所有有工作区坐标的管道都可打开——任务条目用 taskId（任务镜像
        // 解析），非任务条目用 pipelineId（state 行解析通道）
        const wsId = entry.taskId || entry.pipelineId || entry.key
        if (!wsId) return
        openWorkspaceTab(wsId, entry.name)
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
    <div ref={rootRef} className="flex h-full flex-col">
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

      {/* 降级留痕（FE6/FE9 + OBS-R258-1 副面声明）：states/任务列表/项目登记
          拉取失败时提示，不伪装成"无数据"；项目登记面带手动重试 */}
      {(registryStatesError || tasksError || projectsResource.isError) && (
        <div className="border-b bg-status-warning/10 px-3 py-1.5 text-[11px] text-status-warning">
          {registryStatesError && (
            <div>管道状态拉取失败（{registryStatesError.message}）——phase/任务状态可能不全，30s 后自动重试。</div>
          )}
          {tasksError && (
            <div>任务列表不可用（{tasksError.message}）——管道列表仍可展示，30s 后自动重试。</div>
          )}
          {projectsResource.isError && (
            <div className="flex items-center gap-2">
              <span className="flex-1">
                项目登记拉取失败（{projectsResource.error}）——项目分组暂不可用，任务列表不受影响。
              </span>
              <button
                type="button"
                onClick={projectsResource.refetch}
                className="hover:text-foreground shrink-0 underline"
                aria-label="重试拉取项目登记"
              >
                重试
              </button>
            </div>
          )}
        </div>
      )}

      {/* 管道管理视图（首载中不渲染「暂无」空态——四态约定：空 ≠ 加载中） */}
      <div className="min-h-0 flex-1 overflow-auto">
        {listLoading && pipelineEntries.length === 0 ? (
          <div className="text-muted-foreground px-4 py-6 text-center text-xs">
            <Loader2 className="mr-1.5 inline h-3.5 w-3.5 animate-spin" />
            加载中...
          </div>
        ) : viewMode === 'list' ? (
          <PipelineTable
            entries={filteredPipelineEntries}
            nowMs={nowMs}
            expandedKeys={detailOpenKeys}
            locatedKey={locatedKey}
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
            locatedKey={locatedKey}
            onTreeToggle={toggleTreeNode}
            onToggleDetail={toggleDetail}
            onEntryClick={handleEntryClick}
            onAction={handleAction}
          />
        )}
      </div>

      {/* 项目删除确认弹窗（删除口径二选一 + 文件夹勾选） */}
      <Dialog
        open={!!projectDeleteTarget}
        onOpenChange={(open) => {
          if (!open && !deletingProject) setProjectDeleteTarget(null)
        }}
      >
        <DialogContent className="max-w-[420px]">
          <DialogHeader>
            <DialogTitle>删除项目</DialogTitle>
            <DialogDescription>
              确定要删除项目「{projectDeleteTarget?.name}」吗？请选择名下子任务的处置方式：
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2 text-sm">
            <label className="flex cursor-pointer items-start gap-2">
              <input
                type="radio"
                name="project-delete-scope"
                checked={!deleteChildren}
                onChange={() => setDeleteChildren(false)}
                className="accent-primary mt-0.5"
              />
              <span>仅删除项目——名下子任务挂起并保留</span>
            </label>
            <label className="flex cursor-pointer items-start gap-2">
              <input
                type="radio"
                name="project-delete-scope"
                checked={deleteChildren}
                onChange={() => setDeleteChildren(true)}
                className="accent-primary mt-0.5"
              />
              <span>连同子任务一起删除（不可恢复）</span>
            </label>
            <label className="border-border flex cursor-pointer items-start gap-2 border-t pt-2">
              <input
                type="checkbox"
                checked={deleteFiles}
                onChange={(e) => setDeleteFiles(e.target.checked)}
                className="accent-primary mt-0.5"
              />
              <span>同时删除项目文件夹（不可恢复）</span>
            </label>
          </div>
          <DialogFooter>
            <Button
              variant="outline"
              size="sm"
              onClick={() => setProjectDeleteTarget(null)}
              disabled={deletingProject}
            >
              取消
            </Button>
            <Button
              variant="destructive"
              size="sm"
              onClick={handleProjectDeleteConfirm}
              disabled={deletingProject}
            >
              {deletingProject ? (
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
}

// ═════════════════════════════════════════════════════════════════
// 树视图：执行中/最近完成两组，组内按会话分组，孤儿平铺
// ═════════════════════════════════════════════════════════════════

/** 管道树节点：管道条目（任务一对一绑定时条目行即任务行，子级直挂其下） */