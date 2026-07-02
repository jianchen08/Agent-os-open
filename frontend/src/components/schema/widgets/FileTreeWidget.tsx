/** 通用树形组件 根据 Schema 渲染树形结构，支持递归嵌套、展开/折叠、状态图标、 */

import {
  ChevronRight,
  FolderOpen,
  Folder,
  File,
  Search,
  FolderTree,
  CircleDot,
  CheckCircle2,
  XCircle,
  Ban,
  PauseCircle,
  ClipboardList,
  PlayCircle,
  Loader2,
  AlertCircle,
  MessageSquare,
  ExternalLink,
  ArrowUpDown,
  Plus,
} from 'lucide-react'
import React, { useState, useCallback, useMemo, useEffect, useRef } from 'react'
import apiClient from '@/services/api/client'
import { pauseTask, resumeTask } from '@/services/api/tasks'
import { Button } from '@/components/ui/button'
import { CreateTaskModal } from './CreateTaskModal'
import { parseDataSourceRef, resolveDataSource } from '@/services/schema/parser'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useWorkspaceStore } from '@/stores/workspaceStore'
import {
  FileTreeContextMenu,
  type ContextMenuContext,
  type ContextMenuTreeNode,
} from './FileTreeContextMenu'

/** 树展开状态的 localStorage 持久化工具 */
const TREE_EXPANDED_PREFIX = 'tree_expanded_'

/** 获取树展开状态的 localStorage key */
function getExpandedStorageKey(treeKey: string): string {
  return `${TREE_EXPANDED_PREFIX}${treeKey}`
}

/** 保存展开节点 ID 集合到 localStorage */
function saveExpandedIds(treeKey: string, ids: Set<string>): void {
  try {
    localStorage.setItem(getExpandedStorageKey(treeKey), JSON.stringify([...ids]))
  } catch { /* ignore */ }
}

/** 从 localStorage 读取展开节点 ID 集合 */
function loadExpandedIds(treeKey: string): Set<string> | null {
  try {
    const raw = localStorage.getItem(getExpandedStorageKey(treeKey))
    if (!raw) return null
    const arr = JSON.parse(raw)
    if (Array.isArray(arr)) return new Set(arr)
  } catch { /* ignore */ }
  return null
}

/** 树节点数据结构 */
interface TreeNodeData {
  /** 节点唯一标识 */
  id?: string
  /** 节点标题 */
  title?: string
  /** 节点图标 */
  icon?: string
  /** 节点状态 */
  status?: string
  /** 进度值（0-100） */
  progress?: number
  /** 子节点 */
  children?: TreeNodeData[]
  /** 节点描述 */
  description?: string
  /** 其他扩展字段 */
  [key: string]: unknown
}

/** 状态显示配置项 */
interface StatusConfigItem {
  /** 图标名称 */
  icon: string
  /** 颜色类名 */
  color: string
  /** 状态标签 */
  label: string
}

/** 树形组件配置属性 */
interface TreeWidgetConfig {
  /** 树标题 */
  title?: string
  /** 是否显示状态图标 */
  showStatus?: boolean
  /** 是否显示进度条 */
  showProgress?: boolean
  /** 默认展开层级（0=全折叠, -1=全展开） */
  expandLevel?: number
  /** 节点图标字段名 */
  nodeIconField?: string
  /** 节点标题字段名 */
  nodeTitleField?: string
  /** 节点状态字段名 */
  nodeStatusField?: string
  /** 子节点字段名 */
  nodeChildrenField?: string
  /** 状态显示配置 */
  statusConfig?: Record<string, StatusConfigItem>
  /** 直接传入的树数据 */
  data?: TreeNodeData[]
  /** 是否显示搜索框 */
  showSearch?: boolean
  /** 是否显示状态筛选器（默认跟随 showStatus） */
  showStatusFilter?: boolean
  /** 默认筛选状态值（默认 'running'，空字符串表示显示全部） */
  defaultStatusFilter?: string
  /** 是否显示启用/禁用开关（默认 true，workspace:// 数据源自动 false） */
  showEnabledToggle?: boolean
  /** 节点点击回调（用于外部处理节点点击事件） */
  onNodeClick?: (node: TreeNodeData) => void
  /** 文件节点点击回调（用于打开文件编辑器） */
  onFileClick?: (filePath: string, fileName: string) => void
  /** 会话 ID（用于按会话过滤/加载任务数据） */
  sessionId?: string
}

/** 默认状态配置映射 */
const DEFAULT_STATUS_CONFIG: Record<string, StatusConfigItem> = {
  pending: { icon: 'clock', color: 'text-status-warning', label: '待处理' },
  running: { icon: 'loader', color: 'text-status-info', label: '进行中' },
  completed: { icon: 'check', color: 'text-status-success', label: '已完成' },
  failed: { icon: 'x-circle', color: 'text-status-error', label: '失败' },
  blocked: { icon: 'ban', color: 'text-status-running', label: '已阻塞' },
  suspended: { icon: 'pause', color: 'text-status-pending', label: '已暂停' },
  planning: { icon: 'clipboard', color: 'text-status-info', label: '规划中' },
  running: { icon: 'play', color: 'text-status-info', label: '运行中' },
  paused: { icon: 'pause', color: 'text-status-pending', label: '已暂停' },
}

/** 状态筛选选项（用于任务树状态筛选器） */
const STATUS_FILTER_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'running', label: '运行中' },
  { value: 'pending', label: '待处理' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'paused', label: '已暂停' },
  { value: 'blocked', label: '已阻塞' },
] as const

/** 活跃状态集合（running/pending/evaluating/planning） 用于默认筛选模式，仅显示正在执行的任务 */
const ACTIVE_STATUSES_FOR_FILTER = new Set([
  'running',
  'pending',
  'evaluating',
  'planning',
])

/** 递归按状态过滤树节点 过滤策略：保留自身或任意后代匹配状态的节点，保持树状结构不变。 */
function filterNodesByStatus(
  nodes: TreeNodeData[],
  statusValue: string,
  statusField: string,
  childrenField: string,
): TreeNodeData[] {
  if (!statusValue) return nodes

  const result: TreeNodeData[] = []

  for (const node of nodes) {
    const nodeStatus = String(getNodeField(node, statusField) ?? '')
    const children = getNodeField(node, childrenField) as TreeNodeData[] | undefined
    const filteredChildren = children
      ? filterNodesByStatus(children, statusValue, statusField, childrenField)
      : []

    // 判断当前节点状态是否匹配
    const statusMatch =
      statusValue === '__active__'
        ? ACTIVE_STATUSES_FOR_FILTER.has(nodeStatus)
        : nodeStatus === statusValue

    // 自身匹配 或 有匹配的后代 → 保留此节点（保持树结构）
    if (statusMatch || filteredChildren.length > 0) {
      result.push({
        ...node,
        [childrenField]: filteredChildren.length > 0 ? filteredChildren : children,
      })
    }
  }

  return result
}


/** 从 props 中提取树形组件配置 */
function extractTreeConfig(rawProps: Record<string, unknown>): TreeWidgetConfig {
  const nestedProps = rawProps.props
  if (typeof nestedProps === 'object' && nestedProps !== null) {
    return nestedProps as TreeWidgetConfig
  }
  return rawProps as unknown as TreeWidgetConfig
}

/** 从 props 中提取树节点数据 优先使用 props.data，其次使用 props.items */
function extractTreeData(rawProps: Record<string, unknown>): TreeNodeData[] {
  const config = extractTreeConfig(rawProps)
  if (Array.isArray(config.data)) return config.data
  if (Array.isArray(rawProps.data)) return rawProps.data as TreeNodeData[]
  if (Array.isArray(rawProps.items)) return rawProps.items as TreeNodeData[]
  return []
}

/** 获取节点指定字段的值 */
function getNodeField(node: TreeNodeData, field: string): unknown {
  return node[field]
}

/** 获取节点稳定唯一标识 优先使用 node.id，其次使用 node.path（文件树场景）， */
function getStableNodeId(node: TreeNodeData): string {
  if (node.id) return node.id
  const path = node.path as string | undefined
  if (path) return path
  const title = (node.title ?? node.name) as string | undefined
  if (title) return String(title)
  return String(Math.random())
}

/** 根据状态配置获取状态图标组件 */
function getStatusIcon(
  status: string,
  config: Record<string, StatusConfigItem>,
): { icon: React.ReactNode; color: string; label: string } {
  const statusConf = config[status]
  if (!statusConf) {
    return { icon: <CircleDot className="h-4 w-4" />, color: 'text-status-pending', label: status }
  }

  const color = statusConf.color
  const iconMap: Record<string, React.ReactNode> = {
    clock: <AlertCircle className={`h-4 w-4 ${color}`} />,
    loader: <Loader2 className={`h-4 w-4 ${color} animate-spin`} />,
    check: <CheckCircle2 className={`h-4 w-4 ${color}`} />,
    'x-circle': <XCircle className={`h-4 w-4 ${color}`} />,
    ban: <Ban className={`h-4 w-4 ${color}`} />,
    pause: <PauseCircle className={`h-4 w-4 ${color}`} />,
    clipboard: <ClipboardList className={`h-4 w-4 ${color}`} />,
    play: <PlayCircle className={`h-4 w-4 ${color}`} />,
  }

  return {
    icon: iconMap[statusConf.icon] ?? <CircleDot className={`h-4 w-4 ${color}`} />,
    color,
    label: statusConf.label,
  }
}

/** 根据 text-* 颜色类名返回对应的半透明背景色类名，提高状态标签对比度 */
function statusBgClass(colorClass: string): string {
  const bgMap: Record<string, string> = {
    'text-status-success': 'bg-status-success/15',
    'text-status-error': 'bg-status-error/15',
    'text-status-warning': 'bg-status-warning/15',
    'text-status-info': 'bg-status-info/15',
    'text-status-running': 'bg-status-running/15',
    'text-status-pending': 'bg-status-pending/15',
  }
  return bgMap[colorClass] ?? ''
}

/** 递归收集所有节点 ID（用于初始展开） */
function collectExpandedIds(
  nodes: TreeNodeData[],
  childrenField: string,
  maxLevel: number,
  currentLevel: number = 0,
): Set<string> {
  const ids = new Set<string>()

  for (const node of nodes) {
    const children = getNodeField(node, childrenField) as TreeNodeData[] | undefined
    if (!children || children.length === 0) continue

    const nodeId = getStableNodeId(node)
    if (maxLevel === -1 || currentLevel < maxLevel) {
      ids.add(nodeId)
      const childIds = collectExpandedIds(children, childrenField, maxLevel, currentLevel + 1)
      for (const id of childIds) {
        ids.add(id)
      }
    }
  }

  return ids
}

/** 递归收集节点及其所有后代节点的 ID 用于级联开关：当切换一个节点时，其所有子节点也需要同步切换 */
function collectDescendantIds(
  nodes: TreeNodeData[],
  childrenField: string,
): string[] {
  const ids: string[] = []
  for (const node of nodes) {
    const childId = getStableNodeId(node)
    ids.push(childId)
    const children = getNodeField(node, childrenField) as TreeNodeData[] | undefined
    if (children && children.length > 0) {
      ids.push(...collectDescendantIds(children, childrenField))
    }
  }
  return ids
}

/** 在树中查找目标节点的直接子节点列表 */
function findChildrenById(
  nodes: TreeNodeData[],
  targetId: string,
  childrenField: string,
): TreeNodeData[] | null {
  for (const node of nodes) {
    const nodeId = getStableNodeId(node)
    if (nodeId === targetId) {
      return (getNodeField(node, childrenField) as TreeNodeData[] | undefined) ?? null
    }
    const children = getNodeField(node, childrenField) as TreeNodeData[] | undefined
    if (children) {
      const result = findChildrenById(children, targetId, childrenField)
      if (result !== null) return result
    }
  }
  return null
}

/** 递归过滤匹配搜索关键词的节点 */
function filterNodes(
  nodes: TreeNodeData[],
  keyword: string,
  titleField: string,
  childrenField: string,
): TreeNodeData[] {
  if (!keyword.trim()) return nodes

  const lowerKeyword = keyword.toLowerCase()
  const result: TreeNodeData[] = []

  for (const node of nodes) {
    const title = String(getNodeField(node, titleField) ?? '').toLowerCase()
    const children = getNodeField(node, childrenField) as TreeNodeData[] | undefined
    const filteredChildren = children ? filterNodes(children, keyword, titleField, childrenField) : []

    if (title.includes(lowerKeyword) || filteredChildren.length > 0) {
      result.push({
        ...node,
        [childrenField]: filteredChildren.length > 0 ? filteredChildren : children,
      })
    }
  }

  return result
}

/** 排序模式 */
type SortMode = 'none' | 'name-asc' | 'name-desc' | 'type'

/** 递归排序树节点 */
function sortNodes(
  nodes: TreeNodeData[],
  mode: SortMode,
  titleField: string,
  childrenField: string,
): TreeNodeData[] {
  const sorted = [...nodes].sort((a, b) => {
    const titleA = String(getNodeField(a, titleField) ?? '').toLowerCase()
    const titleB = String(getNodeField(b, titleField) ?? '').toLowerCase()
    const childrenA = getNodeField(a, childrenField) as TreeNodeData[] | undefined
    const childrenB = getNodeField(b, childrenField) as TreeNodeData[] | undefined
    const isDirA = Array.isArray(childrenA)
    const isDirB = Array.isArray(childrenB)

    if (isDirA !== isDirB) return isDirA ? -1 : 1

    const cmp = titleA.localeCompare(titleB)
    if (mode === 'name-desc') return -cmp
    return cmp
  })

  return sorted.map((node) => {
    const children = getNodeField(node, childrenField) as TreeNodeData[] | undefined
    if (!Array.isArray(children) || children.length === 0) return node
    return { ...node, [childrenField]: sortNodes(children, mode, titleField, childrenField) }
  })
}

/** 通用树形组件 支持递归嵌套、展开/折叠动画、状态图标、进度条、 */
export function FileTreeWidget(rawProps: Record<string, unknown>) {
  const config = extractTreeConfig(rawProps)
  const allData = extractTreeData(rawProps)

  /** 树标题 */
  const title = config.title ?? (rawProps.title as string | undefined)
  /** 是否显示状态图标 */
  const showStatus = config.showStatus ?? true
  /** 是否显示进度条 */
  const showProgress = config.showProgress ?? false
  /** 默认展开层级 */
  const expandLevel = config.expandLevel ?? -1
  /** 节点图标字段名 */
  const nodeIconField = config.nodeIconField ?? 'icon'
  /** 节点标题字段名 */
  const nodeTitleField = config.nodeTitleField ?? 'title'
  /** 节点状态字段名 */
  const nodeStatusField = config.nodeStatusField ?? 'status'
  /** 子节点字段名 */
  const nodeChildrenField = config.nodeChildrenField ?? 'children'
  /** 是否显示搜索框 */
  const showSearch = config.showSearch ?? false

  /** 是否显示状态筛选器（默认跟随 showStatus） */
  const showStatusFilter = config.showStatusFilter ?? showStatus
  /** 默认筛选状态值（默认 'running'，仅显示正在运行的任务） */
  const defaultStatusFilterValue = config.defaultStatusFilter ?? 'running'

  /** 是否显示启用/禁用开关（workspace:// 数据源默认隐藏） */
  const ds = rawProps.dataSource as string | undefined
  const showEnabledToggle = config.showEnabledToggle ?? !(ds?.startsWith('workspace://'))
  /** 合并状态配置 */
  const statusConfig = { ...DEFAULT_STATUS_CONFIG, ...(config.statusConfig ?? {}) }
  /** 节点点击回调 */
  const onNodeClick = config.onNodeClick ?? (rawProps.onNodeClick as ((node: TreeNodeData) => void) | undefined)
  /** 文件节点点击回调 */
  const onFileClick = config.onFileClick ?? (rawProps.onFileClick as ((filePath: string, fileName: string) => void) | undefined)
  /** 会话 ID */
  const sessionId = config.sessionId ?? (rawProps.sessionId as string | undefined)
  /** 树的唯一标识（用于 localStorage 持久化展开状态） */
  const treeKey = `${sessionId ?? 'default'}_${title ?? 'untitled'}`
  /** 从 localStorage 恢复的展开状态（null 表示无保存记录，需用默认值） */
  const restoredExpandedIds = useMemo(() => loadExpandedIds(treeKey), [treeKey])
  /** 刷新 key（WebSocket 连接状态变化时更新，触发任务树重新加载） */
  const refreshKey = (rawProps.refreshKey as string) ?? ''

  /** 远程加载的树数据（sessionId 驱动） */
  const [remoteTreeData, setRemoteTreeData] = useState<TreeNodeData[]>([])
  /** 是否正在加载远程数据 */
  const [isLoadingRemote, setIsLoadingRemote] = useState(false)
  /** 内部刷新计数器（暂停/恢复操作后递增以触发重新加载） */
  const [internalRefresh, setInternalRefresh] = useState(0)
  /** 标记是否已完成首次加载，用于区分首次加载与刷新，避免刷新时闪烁 loading */
  const hasLoadedRef = useRef(false)

  /** 触发树数据刷新（暂停/恢复操作后调用） */
  const triggerRefresh = useCallback(() => {
    setInternalRefresh((prev) => prev + 1)
  }, [])

  /** 新建根任务模态框开关 */
  const [isCreateTaskOpen, setIsCreateTaskOpen] = useState(false)

  /** 右键上下文菜单状态 */
  const [contextMenu, setContextMenu] = useState<{
    x: number
    y: number
    context: ContextMenuContext
  } | null>(null)

  /** 从 rawProps 获取 containerTaskId（用于文件操作 API） */
  const containerTaskId = rawProps.containerTaskId as string
    ?? (rawProps.dataSource as string | undefined)?.replace('workspace://', '')?.split('/')[0]
    ?? ''

  /** 将 TreeNodeData[] 转换为 ContextMenuTreeNode[]（供上下文菜单移动对话框使用） */
  const toContextMenuTree = useCallback(
    (nodes: TreeNodeData[]): ContextMenuTreeNode[] =>
      nodes.map((n) => ({
        name: String(getNodeField(n, nodeTitleField) ?? ''),
        path: (n.path as string) ?? getStableNodeId(n),
        isDirectory: !!(getNodeField(n, nodeChildrenField) as TreeNodeData[] | undefined)?.length
          || (n.type as string) === 'directory',
        children: (getNodeField(n, nodeChildrenField) as TreeNodeData[] | undefined)
          ?.map((c) => ({
            name: String(getNodeField(c, nodeTitleField) ?? ''),
            path: (c.path as string) ?? getStableNodeId(c),
            isDirectory: !!(getNodeField(c, nodeChildrenField) as TreeNodeData[] | undefined)?.length
              || (c.type as string) === 'directory',
            children: undefined,
          })),
      })),
    [nodeTitleField, nodeChildrenField],
  )

  /** 实际使用的树数据：优先使用远程数据，否则使用直接传入的数据 */
  const effectiveData = remoteTreeData.length > 0 ? remoteTreeData : allData

  /** 处理节点右键菜单 */
  const handleNodeContextMenu = useCallback(
    (e: React.MouseEvent, node: TreeNodeData) => {
      e.preventDefault()
      e.stopPropagation()
      const nodePath = (node.path as string) ?? getStableNodeId(node)
      const nodeTitle = String(getNodeField(node, nodeTitleField) ?? '')
      const children = getNodeField(node, nodeChildrenField) as TreeNodeData[] | undefined
      const isDir = !!children?.length || (node.type as string) === 'directory'
      const parentPath = nodePath.includes('/') ? nodePath.substring(0, nodePath.lastIndexOf('/')) : ''
      setContextMenu({
        x: e.clientX,
        y: e.clientY,
        context: {
          containerTaskId,
          targetPath: nodePath,
          targetName: nodeTitle,
          isDirectory: isDir,
          parentDir: isDir ? nodePath : parentPath,
          treeData: toContextMenuTree(effectiveData),
          onRefresh: triggerRefresh,
        },
      })
    },
    [containerTaskId, nodeTitleField, nodeChildrenField, effectiveData, toContextMenuTree, triggerRefresh],
  )

  /** 处理空白区域右键菜单 */
  const handleBlankContextMenu = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault()
      if (!containerTaskId) return
      setContextMenu({
        x: e.clientX,
        y: e.clientY,
        context: {
          containerTaskId,
          targetPath: null,
          targetName: null,
          isDirectory: false,
          parentDir: '',
          treeData: toContextMenuTree(effectiveData),
          onRefresh: triggerRefresh,
        },
      })
    },
    [containerTaskId, effectiveData, toContextMenuTree, triggerRefresh],
  )

  /** 从 API 加载任务树数据 */
  useEffect(() => {
    if (!rawProps.dataSource) {
      setRemoteTreeData([])
      return
    }

    let cancelled = false

    const loadTreeData = async () => {
      if (!hasLoadedRef.current) {
        setIsLoadingRemote(true)
      }
      try {
        const ref = parseDataSourceRef(rawProps.dataSource as string)
        const resolved = resolveDataSource(ref)
        const params: Record<string, string> = { ...resolved.params as Record<string, string> }
        if (sessionId) {
          params.session_id = sessionId
        }
        const response = await apiClient.get(resolved.endpoint, { params })
        if (cancelled) return
        const raw = response.data
        const tree = raw?.children ?? raw?.tree ?? []
        const flat = raw?.items ?? []
        const filteredData = tree.length > 0 ? tree : flat

        if (filteredData.length > 0) {
          setRemoteTreeData(filteredData)
        } else if (sessionId) {
          const fallbackParams: Record<string, string> = { ...resolved.params as Record<string, string> }
          const fallbackResp = await apiClient.get(resolved.endpoint, { params: fallbackParams })
          if (cancelled) return
          const fallbackRaw = fallbackResp.data
          const fallbackTree = fallbackRaw?.children ?? fallbackRaw?.tree ?? []
          const fallbackFlat = fallbackRaw?.items ?? []
          setRemoteTreeData(fallbackTree.length > 0 ? fallbackTree : fallbackFlat)
        } else {
          setRemoteTreeData([])
        }
        hasLoadedRef.current = true
      } catch {
        if (!cancelled) {
          setRemoteTreeData([])
        }
      } finally {
        if (!cancelled) {
          setIsLoadingRemote(false)
        }
      }
    }

    const debounceMs = hasLoadedRef.current ? 500 : 0
    const timer = setTimeout(() => {
      if (!cancelled) loadTreeData()
    }, debounceMs)

    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [sessionId, rawProps.dataSource, refreshKey, internalRefresh])

  /** sessionId 变更时重置首次加载标记 */
  useEffect(() => {
    hasLoadedRef.current = false
  }, [sessionId])

  /** 选中节点 ID */
  const [selectedId, setSelectedId] = useState<string | null>(null)
  /** 搜索关键词 */
  const [searchKeyword, setSearchKeyword] = useState('')
  /** 展开的节点 ID 集合（优先从 localStorage 恢复） */
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => restoredExpandedIds ?? new Set<string>())
  /** 状态筛选值（默认 'running'，空字符串表示显示全部） */
  const [statusFilter, setStatusFilter] = useState(defaultStatusFilterValue)
  /** 节点启用/禁用状态映射（true=启用，false=禁用） */
  const [enabledMap, setEnabledMap] = useState<Record<string, boolean>>({})
  /** 已知的任务 ID 集合（用于检测新提交的任务并自动开启开关） */
  const knownTaskIdsRef = useRef<Set<string>>(new Set())
  /** 正在切换启用/禁用状态的节点 ID 集合 */
  const [togglingIds, setTogglingIds] = useState<Set<string>>(new Set())
  /** 数据变更追踪 */
  const prevDataRef = useRef<TreeNodeData[]>([])
  const hasInitializedRef = useRef(false)

  /** treeKey 变更时重新从 localStorage 恢复展开状态 修复场景：sessionId 在组件挂载后才从 sessionStore 异步加载完成， */
  const prevTreeKeyRef = useRef(treeKey)
  const skipNextSaveRef = useRef(false)
  useEffect(() => {
    if (prevTreeKeyRef.current === treeKey) return
    prevTreeKeyRef.current = treeKey
    skipNextSaveRef.current = true
    const restored = loadExpandedIds(treeKey)
    setExpandedIds(restored ?? new Set<string>())
    hasInitializedRef.current = false
    prevDataRef.current = []
    seenNodeIdsRef.current = new Set()
  }, [treeKey])

  /** 检测新提交的任务并自动开启开关。 逻辑：首次加载时仅记录所有任务 ID（不开启开关，重启后默认全部关闭）； */
  useEffect(() => {
    if (effectiveData.length === 0) return

    const currentIds = new Set<string>()
    const collectIds = (nodes: TreeNodeData[]) => {
      for (const node of nodes) {
        currentIds.add(getStableNodeId(node))
        const children = getNodeField(node, nodeChildrenField) as TreeNodeData[] | undefined
        if (children) collectIds(children)
      }
    }
    collectIds(effectiveData)

    const known = knownTaskIdsRef.current
    if (known.size === 0) {
      knownTaskIdsRef.current = currentIds
      return
    }

    const newIds: string[] = []
    for (const id of currentIds) {
      if (!known.has(id)) {
        newIds.push(id)
      }
    }

    if (newIds.length > 0) {
      setEnabledMap((prev) => {
        const next = { ...prev }
        for (const id of newIds) {
          next[id] = true
        }
        return next
      })
    }

    knownTaskIdsRef.current = currentIds
  }, [effectiveData, nodeChildrenField])

  /** 已出现过的节点 ID 集合（用于区分新老节点） */
  const seenNodeIdsRef = useRef<Set<string>>(new Set())

  /** 收集树中所有节点 ID */
  const collectAllNodeIds = useCallback((nodes: TreeNodeData[]): Set<string> => {
    const ids = new Set<string>()
    const walk = (list: TreeNodeData[]) => {
      for (const node of list) {
        ids.add(getStableNodeId(node))
        const children = getNodeField(node, nodeChildrenField) as TreeNodeData[] | undefined
        if (children) walk(children)
      }
    }
    walk(nodes)
    return ids
  }, [nodeChildrenField])

  /** 当 effectiveData 变化时计算展开节点 策略： */
  useEffect(() => {
    if (effectiveData.length === 0) return
    if (effectiveData === prevDataRef.current) return
    prevDataRef.current = effectiveData

    if (!hasInitializedRef.current) {
      hasInitializedRef.current = true
      const currentIds = collectAllNodeIds(effectiveData)
      seenNodeIdsRef.current = currentIds
      if (restoredExpandedIds) {
        return
      }
      setExpandedIds(collectExpandedIds(effectiveData, nodeChildrenField, expandLevel))
      return
    }

    const currentIds = collectAllNodeIds(effectiveData)
    const prevIds = seenNodeIdsRef.current
    seenNodeIdsRef.current = currentIds

    const trulyNewIds = new Set<string>()
    for (const id of currentIds) {
      if (!prevIds.has(id)) {
        trulyNewIds.add(id)
      }
    }

    if (trulyNewIds.size === 0) return

    const defaultExpanded = collectExpandedIds(effectiveData, nodeChildrenField, expandLevel)
    setExpandedIds((prev) => {
      const merged = new Set(prev)
      let changed = false
      for (const id of defaultExpanded) {
        if (trulyNewIds.has(id) && !merged.has(id)) {
          merged.add(id)
          changed = true
        }
      }
      return changed ? merged : prev
    })
  }, [effectiveData, nodeChildrenField, expandLevel, restoredExpandedIds, collectAllNodeIds])

  /** 展开状态变化时自动持久化到 localStorage 当 treeKey 刚变更时跳过首次保存，避免用临时展开状态 */
  useEffect(() => {
    if (skipNextSaveRef.current) {
      skipNextSaveRef.current = false
      return
    }
    if (expandedIds.size > 0 || hasInitializedRef.current) {
      saveExpandedIds(treeKey, expandedIds)
    }
  }, [expandedIds, treeKey])

  /** 排序模式 */
  const [sortMode, setSortMode] = useState<SortMode>('none')

  /** 排序模式循环切换标签 */
  const SORT_CYCLE: SortMode[] = ['none', 'name-asc', 'name-desc', 'type']
  const SORT_LABELS: Record<SortMode, string> = {
    none: '默认排序',
    'name-asc': '名称 A→Z',
    'name-desc': '名称 Z→A',
    type: '文件夹优先',
  }

  /** 过滤 + 排序后的数据 */
  const filteredData = useMemo(() => {
    // 1. 按状态筛选（保持树结构）
    const statusFiltered = showStatusFilter && statusFilter
      ? filterNodesByStatus(effectiveData, statusFilter, nodeStatusField, nodeChildrenField)
      : effectiveData
    // 2. 按搜索关键词过滤
    const keywordFiltered = filterNodes(statusFiltered, searchKeyword, nodeTitleField, nodeChildrenField)
    // 3. 排序
    return sortNodes(keywordFiltered, sortMode, nodeTitleField, nodeChildrenField)
  }, [effectiveData, searchKeyword, sortMode, nodeTitleField, nodeChildrenField, showStatusFilter, statusFilter, nodeStatusField])

  /** 切换排序模式 */
  const handleSortToggle = useCallback(() => {
    setSortMode((prev) => {
      const idx = SORT_CYCLE.indexOf(prev)
      return SORT_CYCLE[(idx + 1) % SORT_CYCLE.length]
    })
  }, [])

  /** 切换节点展开/折叠状态 */
  const handleToggle = useCallback((nodeId: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev)
      if (next.has(nodeId)) {
        next.delete(nodeId)
      } else {
        next.add(nodeId)
      }
      return next
    })
  }, [])

  /** 处理节点选中 */
  const handleSelect = useCallback((nodeId: string) => {
    setSelectedId(nodeId)
  }, [])

  /** 处理搜索关键词变化 */
  const handleSearchChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setSearchKeyword(e.target.value)
  }, [])

  /** 级联切换节点启用/禁用状态（调用后端 API + 刷新数据） 开关状态完全由后端任务状态驱动： */
  const handleToggleEnabled = useCallback(
    async (nodeId: string, enabled: boolean) => {
      setTogglingIds((prev) => {
        const next = new Set(prev)
        next.add(nodeId)
        const children = findChildrenById(effectiveData, nodeId, nodeChildrenField)
        if (children && children.length > 0) {
          const descendantIds = collectDescendantIds(children, nodeChildrenField)
          for (const id of descendantIds) {
            next.add(id)
          }
        }
        return next
      })

      try {
        if (enabled) {
          await resumeTask(nodeId)
        } else {
          await pauseTask(nodeId)
        }
        const children = findChildrenById(effectiveData, nodeId, nodeChildrenField)
        if (children && children.length > 0) {
          const descendantIds = collectDescendantIds(children, nodeChildrenField)
          const apiCall = enabled ? resumeTask : pauseTask
          await Promise.allSettled(descendantIds.map((id) => apiCall(id)))
        }
        triggerRefresh()
      } catch (err) {
        console.error('[FileTreeWidget] toggle enabled failed:', err)
        triggerRefresh()
      } finally {
        setTogglingIds((prev) => {
          const next = new Set(prev)
          next.delete(nodeId)
          const children = findChildrenById(effectiveData, nodeId, nodeChildrenField)
          if (children && children.length > 0) {
            const descendantIds = collectDescendantIds(children, nodeChildrenField)
            for (const id of descendantIds) {
              next.delete(id)
            }
          }
          return next
        })
      }
    },
    [effectiveData, nodeChildrenField, triggerRefresh],
  )

  /** 空状态渲染 */
  if (effectiveData.length === 0 && !isLoadingRemote) {
    return (
      <div className="w-full rounded-lg border">
        <div className="flex flex-col items-center justify-center p-8">
          <FolderTree className="text-muted-foreground mb-3 h-12 w-12" />
          <p className="text-muted-foreground text-sm">暂无树形数据</p>
          <p className="text-muted-foreground/60 mt-1 text-xs">等待数据加载或配置数据源</p>
        </div>
      </div>
    )
  }

  return (
    <div className="w-full rounded-lg border">
      {/* 搜索框 + 排序 */}

      {/* 状态筛选器 */}
      {showStatusFilter && (
        <div className="border-b px-3 py-2">
          <div className="flex flex-wrap items-center justify-between gap-1">
            <div className="flex flex-wrap items-center gap-1">
              {STATUS_FILTER_OPTIONS.map((opt) => (
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
            <Button
              variant="ghost"
              size="xs"
              onClick={() => setIsCreateTaskOpen(true)}
              title="新建根任务"
            >
              <Plus className="h-3.5 w-3.5" />
              新建任务
            </Button>
          </div>
        </div>
      )}

      {showSearch && (
        <div className="border-b px-3 py-2">
          <div className="flex items-center gap-2">
            <div className="relative flex-1">
              <Search className="text-muted-foreground absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2" />
              <input
                type="text"
                value={searchKeyword}
                onChange={handleSearchChange}
                placeholder="搜索节点..."
                className="bg-muted/50 focus:bg-background w-full rounded-md border py-1.5 pl-7 pr-3 text-xs outline-none transition-colors focus:border-status-info/50"
              />
            </div>
            <button
              onClick={handleSortToggle}
              title={SORT_LABELS[sortMode]}
              className={`flex h-7 w-7 shrink-0 items-center justify-center rounded-md transition-colors ${
                sortMode !== 'none' ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/50 text-muted-foreground'
              }`}
            >
              <ArrowUpDown className="h-3.5 w-3.5" />
            </button>
          </div>
          {sortMode !== 'none' && (
            <div className="text-muted-foreground mt-1 text-[10px]">
              {SORT_LABELS[sortMode]}
            </div>
          )}
        </div>
      )}

      {/* 树节点列表 */}
      <div className="py-1" onContextMenu={handleBlankContextMenu}>
        {filteredData.length === 0 ? (
          <div className="px-4 py-6 text-center">
            <p className="text-muted-foreground text-xs">未找到匹配的节点</p>
          </div>
        ) : (
          filteredData.map((node) => (
            <TreeNode
              key={getStableNodeId(node)}
              node={node}
              depth={0}
              expandedIds={expandedIds}
              selectedId={selectedId}
              showStatus={showStatus}
              showProgress={showProgress}
              showEnabledToggle={showEnabledToggle}
              nodeIconField={nodeIconField}
              nodeTitleField={nodeTitleField}
              nodeStatusField={nodeStatusField}
              nodeChildrenField={nodeChildrenField}
              statusConfig={statusConfig}
              onToggle={handleToggle}
              onSelect={handleSelect}
              onNodeClick={onNodeClick}
              onFileClick={onFileClick}
              onRefresh={triggerRefresh}
              togglingIds={togglingIds}
              enabledMap={enabledMap}
              onToggleEnabled={handleToggleEnabled}
              onContextMenu={handleNodeContextMenu}
            />
          ))
        )}
      </div>

      {/* 上下文菜单 */}
      {contextMenu && (
        <FileTreeContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          context={contextMenu.context}
          onClose={() => setContextMenu(null)}
        />
      )}

      {/* 新建根任务模态框 */}
      <CreateTaskModal
        isOpen={isCreateTaskOpen}
        onClose={() => setIsCreateTaskOpen(false)}
        sessionId={sessionId}
        onCreated={triggerRefresh}
      />
    </div>
  )
}

/** 树节点组件属性 */
interface TreeNodeProps {
  /** 节点数据 */
  node: TreeNodeData
  /** 当前深度 */
  depth: number
  /** 展开的节点 ID 集合 */
  expandedIds: Set<string>
  /** 选中的节点 ID */
  selectedId: string | null
  /** 是否显示状态图标 */
  showStatus: boolean
  /** 是否显示进度条 */
  showProgress: boolean
  /** 是否显示启用/禁用开关 */
  showEnabledToggle: boolean
  /** 节点图标字段名 */
  nodeIconField: string
  /** 节点标题字段名 */
  nodeTitleField: string
  /** 节点状态字段名 */
  nodeStatusField: string
  /** 子节点字段名 */
  nodeChildrenField: string
  /** 状态配置映射 */
  statusConfig: Record<string, StatusConfigItem>
  /** 展开/折叠回调 */
  onToggle: (nodeId: string) => void
  /** 选中回调 */
  onSelect: (nodeId: string) => void
  /** 节点点击回调（用于打开对话） */
  onNodeClick?: (node: TreeNodeData) => void
  /** 文件节点点击回调（用于打开文件编辑器） */
  onFileClick?: (filePath: string, fileName: string) => void
  /** 数据刷新回调（暂停/恢复操作后触发） */
  onRefresh?: () => void
  /** 正在切换中的节点 ID 集合 */
  togglingIds: Set<string>
  /** 节点启用/禁用状态映射 */
  enabledMap: Record<string, boolean>
  /** 级联切换启用/禁用回调 */
  onToggleEnabled: (nodeId: string, enabled: boolean) => void
  /** 右键菜单回调（用于文件操作上下文菜单） */
  onContextMenu?: (e: React.MouseEvent, node: TreeNodeData) => void
}

/** 树节点组件 递归渲染单个树节点及其子节点，处理展开/折叠动画、 */
/** 优先级标签映射 */
const PRIORITY_LABELS: Record<string, { label: string; color: string }> = {
  critical: { label: '紧急', color: 'text-red-500' },
  high: { label: '高', color: 'text-orange-500' },
  normal: { label: '普通', color: 'text-muted-foreground' },
  low: { label: '低', color: 'text-muted-foreground/60' },
}

/** 格式化时间戳为可读字符串 */
function formatTime(value: string | null | undefined): string | null {
  if (!value) return null
  try {
    const d = new Date(value)
    if (isNaN(d.getTime())) return null
    const mm = String(d.getMonth() + 1).padStart(2, '0')
    const dd = String(d.getDate()).padStart(2, '0')
    const hh = String(d.getHours()).padStart(2, '0')
    const mi = String(d.getMinutes()).padStart(2, '0')
    return `${mm}-${dd} ${hh}:${mi}`
  } catch {
    return null
  }
}

function TreeNode({
  node,
  depth,
  expandedIds,
  selectedId,
  showStatus,
  showProgress,
  showEnabledToggle,
  nodeIconField,
  nodeTitleField,
  nodeStatusField,
  nodeChildrenField,
  statusConfig,
  onToggle,
  onSelect,
  onNodeClick,
  onFileClick,
  onRefresh,
  togglingIds,
  enabledMap,
  onToggleEnabled,
  onContextMenu,
}: TreeNodeProps): React.ReactNode {
  const nodeId = getStableNodeId(node)
  const title = String(getNodeField(node, nodeTitleField) ?? '未命名')
  const icon = getNodeField(node, nodeIconField) as string | undefined
  const status = getNodeField(node, nodeStatusField) as string | undefined
  const children = getNodeField(node, nodeChildrenField) as TreeNodeData[] | undefined
  const progress = node.progress as number | undefined
  const description = node.description as string | undefined
  const priority = node.priority as string | undefined
  const createdAt = node.created_at as string | undefined
  const error = node.error as string | undefined
  const pipelineRunId = node.pipeline_run_id as string | undefined
  const wsMode = node.ws_mode as string | undefined
  const wsPath = node.ws_path as string | undefined
  const agentName = node.agent_name as string | undefined
  const hasChildren = Array.isArray(children) && children.length > 0
  const isExpanded = expandedIds.has(nodeId)
  const isSelected = selectedId === nodeId
  const taskScope = node.task_scope as string | undefined
  const isContainer = taskScope === 'container'
  const hasPipeline = !!pipelineRunId && !isContainer
  const hasWorkspace = !!wsMode && wsMode !== 'shared' && !!wsPath

  /** 当前节点是否启用（由后端任务状态驱动） */
  const ACTIVE_STATUSES = new Set(['running', 'pending', 'evaluating', 'planning'])
  const isEnabled = ACTIVE_STATUSES.has(status ?? '')
  const isToggling = togglingIds.has(nodeId)

  /** 是否有元信息需要显示第二行 */
  const hasMeta = error && error.trim().length > 0

  const handleClick = useCallback(() => {
    onSelect(nodeId)
    if (hasChildren) {
      onToggle(nodeId)
    } else if (onFileClick) {
      // 文件节点点击：使用 path 字段作为文件路径，fallback 到 nodeId
      const filePath = (node.path as string) ?? nodeId
      onFileClick(filePath, title)
    }
  }, [nodeId, hasChildren, onToggle, onSelect, onFileClick, node, title])

  const handleChevronClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation()
      onToggle(nodeId)
    },
    [nodeId, onToggle],
  )

  /** 处理对话按钮点击 仅当节点拥有 pipeline_run_id 时才触发对话打开， */
  const handleConversationClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation()
      if (onNodeClick && hasPipeline) {
        onNodeClick(node)
      }
    },
    [onNodeClick, hasPipeline, node],
  )

  /** 处理打开工作空间按钮点击 通过 component-based tab 创建文件树标签，数据由 FileTreeWidget */
  const handleOpenWorkspace = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation()
      if (!nodeId || !wsPath) return
      try {
        const layoutStore = useLayoutModeStore.getState()
        const tabId = `ws-tree-${nodeId}`
        const existingTab = layoutStore.workspaceTabs.find(t => t.id === tabId)
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
          dataSource: `workspace://${nodeId}`,
          isActive: true,
          isPinned: false,
        })
      } catch {
        // 静默失败
      }
    },
    [nodeId, wsPath, title],
  )

  /** 处理开关切换点击 级联切换：将自身及所有下级子任务统一设置为新状态 */
  const handleEnabledToggle = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation()
      if (!nodeId) return
      onToggleEnabled(nodeId, !isEnabled)
    },
    [nodeId, isEnabled, onToggleEnabled],
  )

  const statusInfo = showStatus && status ? getStatusIcon(status, statusConfig) : null

  const clampedProgress =
    typeof progress === 'number' ? Math.max(0, Math.min(100, progress)) : null

  /** 优先级配置 */
  const priorityConf = priority ? PRIORITY_LABELS[priority] ?? PRIORITY_LABELS.normal : null

  /** 格式化创建时间 */
  const formattedTime = formatTime(createdAt)

  /** 是否有操作按钮需要显示 */
  const hasActions = hasPipeline || hasWorkspace

  // 失败/完成/暂停等非活跃任务统一使用 opacity-80（0.8 不透明度）。
  // 注意：opacity 仅作用于"当前节点行"的 div，不能加在包住子节点的外层 div 上，
  // 否则 TreeNode 递归时父子 opacity 会乘法叠加（0.8 × 0.8 × ...），
  // 导致越深的子节点越透明、越看不清。
  const inactiveOpacityClass = showEnabledToggle && !isEnabled ? 'opacity-80' : ''

  return (
    <div
      onContextMenu={(e) => onContextMenu?.(e, node)}
    >
      <div
        className={`group flex cursor-pointer items-start py-1.5 transition-colors hover:bg-accent ${
          isSelected
            ? 'bg-accent/50 border-l-2 border-l-status-info'
            : 'border-l-2 border-l-transparent'
        } ${inactiveOpacityClass}`}
        style={{ paddingLeft: `${depth * 20 + 8}px` }}
        onClick={handleClick}
      >
        <div className="flex shrink-0 items-center pt-0.5">
          {showEnabledToggle && (
          <button
            className={`mr-1.5 flex h-4 w-7 shrink-0 items-center rounded-full p-0.5 transition-colors ${
              isEnabled
                ? 'bg-status-info justify-end'
                : 'bg-muted justify-start'
            }`}
            onClick={handleEnabledToggle}
            title={isEnabled ? '点击禁用（将级联禁用所有子任务）' : '点击启用'}
            tabIndex={-1}
          >
            <div className={`h-3 w-3 rounded-full bg-white shadow-sm transition-transform ${
              isEnabled ? '' : ''
            }`} />
          </button>
          )}
          <button
            className={`mr-1 flex h-5 w-5 items-center justify-center rounded transition-transform ${
              hasChildren
                ? 'text-muted-foreground hover:text-foreground'
                : 'invisible'
            }`}
            onClick={handleChevronClick}
            tabIndex={-1}
          >
            <ChevronRight
              className={`h-3.5 w-3.5 transition-transform duration-200 ${
                isExpanded ? 'rotate-90' : ''
              }`}
            />
          </button>

          {icon ? (
            <span className="mr-1.5 shrink-0 text-sm">{icon}</span>
          ) : hasChildren ? (
            isExpanded ? (
              <FolderOpen className="text-status-warning mr-1.5 h-4 w-4 shrink-0" />
            ) : (
              <Folder className="text-status-warning mr-1.5 h-4 w-4 shrink-0" />
            )
          ) : (
            <File className="text-muted-foreground mr-1.5 h-4 w-4 shrink-0" />
          )}

          {statusInfo && (
            <span className="mr-1.5 shrink-0" title={statusInfo.label}>
              {statusInfo.icon}
            </span>
          )}
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5">
            <span className={`truncate text-sm ${isSelected ? 'text-foreground font-medium' : 'text-foreground/90'}`}>
              {title}
            </span>
            {agentName && agentName.trim() && (
              <span className="shrink-0 rounded bg-primary/10 px-1.5 py-0 text-[10px] text-primary/70">
                {agentName.trim()}
              </span>
            )}
            {statusInfo && (
              <span className={`shrink-0 rounded px-1 text-[10px] font-medium ${statusInfo.color} ${statusBgClass(statusInfo.color)}`}>
                {statusInfo.label}
              </span>
            )}
            {hasChildren && (
              <span className="text-muted-foreground/50 shrink-0 text-[10px]">
                [{children!.length}]
              </span>
            )}
          </div>

          {hasMeta && (
            <div className="mt-0.5 space-y-0.5">
              {error && error.trim().length > 0 && (
                <p className="truncate text-[11px] leading-tight text-status-error">
                  ⚠ {error}
                </p>
              )}
              <div className="flex items-center gap-2 text-[10px] text-muted-foreground/50">
                {priorityConf && priority !== 'normal' && (
                  <span className={priorityConf.color}>{priorityConf.label}</span>
                )}
                {formattedTime && (
                  <span>{formattedTime}</span>
                )}
              </div>
            </div>
          )}

          {showProgress && clampedProgress !== null && (
            <div className="mt-1 flex items-center gap-2">
              <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-muted">
                <div
                  className="h-full rounded-full bg-status-info transition-all duration-500 ease-out"
                  style={{ width: `${clampedProgress}%` }}
                />
              </div>
              <span className="text-muted-foreground shrink-0 text-[10px] tabular-nums">
                {clampedProgress}%
              </span>
            </div>
          )}
        </div>

        {/* 操作按钮区域 */}
        {hasActions && (
          <div className="flex shrink-0 items-center gap-0.5 pr-2 pt-0.5">
            {/* 对话按钮：仅当节点拥有 pipeline_run_id 时显示 */}
            {hasPipeline && (
              <button
                className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                onClick={handleConversationClick}
                title="打开对话"
                tabIndex={-1}
              >
                <MessageSquare className="h-3.5 w-3.5" />
              </button>
            )}

            {/* 打开工作空间按钮：当节点具有独立工作空间时显示 */}
            {hasWorkspace && (
              <button
                className="flex h-6 w-6 items-center justify-center rounded text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                onClick={handleOpenWorkspace}
                title={`打开工作空间: ${wsPath}`}
                tabIndex={-1}
              >
                <ExternalLink className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        )}
      </div>

      {hasChildren && isExpanded && (
        <div>
          {children!.map((child) => (
            <TreeNode
              key={getStableNodeId(child)}
              node={child}
              depth={depth + 1}
              expandedIds={expandedIds}
              selectedId={selectedId}
              showStatus={showStatus}
              showProgress={showProgress}
              showEnabledToggle={showEnabledToggle}
              nodeIconField={nodeIconField}
              nodeTitleField={nodeTitleField}
              nodeStatusField={nodeStatusField}
              nodeChildrenField={nodeChildrenField}
              statusConfig={statusConfig}
              onToggle={onToggle}
              onSelect={onSelect}
              onNodeClick={onNodeClick}
              onFileClick={onFileClick}
              onRefresh={onRefresh}
              togglingIds={togglingIds}
              enabledMap={enabledMap}
              onToggleEnabled={onToggleEnabled}
              onContextMenu={onContextMenu}
            />

          ))}

        </div>
      )}
    </div>
  )

}
