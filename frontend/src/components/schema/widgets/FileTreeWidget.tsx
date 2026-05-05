/**
 * 通用树形组件
 *
 * 根据 Schema 渲染树形结构，支持递归嵌套、展开/折叠、状态图标、
 * 进度条、选中高亮、搜索过滤等功能。
 * 可用于任务树、文件树、组织架构等树形数据展示场景。
 *
 * @module FileTreeWidget
 */

import React, { useState, useCallback, useMemo, useEffect } from 'react'
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
} from 'lucide-react'
import { API_ENDPOINTS } from '@/constants/api'
import apiClient from '@/services/api/client'

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
  /** 节点点击回调（用于外部处理节点点击事件） */
  onNodeClick?: (node: TreeNodeData) => void
  /** 会话 ID（用于按会话过滤/加载任务数据） */
  sessionId?: string
}

/** 默认状态配置映射 */
const DEFAULT_STATUS_CONFIG: Record<string, StatusConfigItem> = {
  pending: { icon: 'clock', color: 'text-status-warning', label: '待处理' },
  in_progress: { icon: 'loader', color: 'text-status-info', label: '进行中' },
  completed: { icon: 'check', color: 'text-status-success', label: '已完成' },
  failed: { icon: 'x-circle', color: 'text-status-error', label: '失败' },
  blocked: { icon: 'ban', color: 'text-status-running', label: '已阻塞' },
  suspended: { icon: 'pause', color: 'text-status-pending', label: '已暂停' },
  planning: { icon: 'clipboard', color: 'text-status-info', label: '规划中' },
  running: { icon: 'play', color: 'text-status-info', label: '运行中' },
  paused: { icon: 'pause', color: 'text-status-pending', label: '已暂停' },
}

/**
 * 从 props 中提取树形组件配置
 *
 * @param rawProps - 原始组件属性
 * @returns 类型安全的树形组件配置
 */
function extractTreeConfig(rawProps: Record<string, unknown>): TreeWidgetConfig {
  const nestedProps = rawProps.props
  if (typeof nestedProps === 'object' && nestedProps !== null) {
    return nestedProps as TreeWidgetConfig
  }
  return rawProps as unknown as TreeWidgetConfig
}

/**
 * 从 props 中提取树节点数据
 *
 * 优先使用 props.data，其次使用 props.items
 *
 * @param rawProps - 原始组件属性
 * @returns 树节点数据数组
 */
function extractTreeData(rawProps: Record<string, unknown>): TreeNodeData[] {
  const config = extractTreeConfig(rawProps)
  if (Array.isArray(config.data)) return config.data
  if (Array.isArray(rawProps.data)) return rawProps.data as TreeNodeData[]
  if (Array.isArray(rawProps.items)) return rawProps.items as TreeNodeData[]
  return []
}

/**
 * 获取节点指定字段的值
 *
 * @param node - 树节点
 * @param field - 字段名
 * @returns 字段值
 */
function getNodeField(node: TreeNodeData, field: string): unknown {
  return node[field]
}

/**
 * 根据状态配置获取状态图标组件
 *
 * @param status - 节点状态
 * @param config - 状态配置映射
 * @returns 图标组件和颜色类名
 */
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

/**
 * 递归收集所有节点 ID（用于初始展开）
 *
 * @param nodes - 树节点数组
 * @param childrenField - 子节点字段名
 * @param maxLevel - 最大展开层级（-1 表示全部）
 * @param currentLevel - 当前层级
 * @returns 需要展开的节点 ID 集合
 */
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

    const nodeId = node.id ?? String(node.title ?? Math.random())
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

/**
 * 递归过滤匹配搜索关键词的节点
 *
 * @param nodes - 原始节点数组
 * @param keyword - 搜索关键词
 * @param titleField - 标题字段名
 * @param childrenField - 子节点字段名
 * @returns 过滤后的节点数组
 */
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

/**
 * 通用树形组件
 *
 * 支持递归嵌套、展开/折叠动画、状态图标、进度条、
 * 选中高亮和搜索过滤，由 Schema 驱动渲染。
 *
 * @param rawProps - 组件属性（由 Schema 传入）
 * @returns 树形组件渲染结果
 */
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
  /** 合并状态配置 */
  const statusConfig = { ...DEFAULT_STATUS_CONFIG, ...(config.statusConfig ?? {}) }
  /** 节点点击回调 */
  const onNodeClick = config.onNodeClick ?? (rawProps.onNodeClick as ((node: TreeNodeData) => void) | undefined)
  /** 会话 ID */
  const sessionId = config.sessionId ?? (rawProps.sessionId as string | undefined)

  /** 远程加载的树数据（sessionId 驱动） */
  const [remoteTreeData, setRemoteTreeData] = useState<TreeNodeData[]>([])
  /** 是否正在加载远程数据 */
  const [isLoadingRemote, setIsLoadingRemote] = useState(false)

  /**
   * 从 API 加载任务树数据
   *
   * BUG-FIX-fix_20260505: 修复任务树无法加载的问题
   * 问题根因: 原逻辑在 sessionId 为 null 时直接清空数据并跳过加载，
   *          导致用户未选择会话时任务树始终为空。
   * 修复方案: 仅在 dataSource 未配置时跳过加载；sessionId 为 null 时
   *          仍发起请求但不传 session_id 参数，由后端返回全部任务数据。
   *
   * @param sessionId - 当前会话 ID，为 null 时不传过滤参数
   * @param rawProps.dataSource - 数据源配置，未配置时跳过加载
   */
  useEffect(() => {
    if (!rawProps.dataSource) {
      setRemoteTreeData([])
      return
    }

    let cancelled = false

    /** 构建 API 请求参数，仅当 sessionId 有效时附加过滤条件 */
    const loadTreeData = async () => {
      setIsLoadingRemote(true)
      try {
        const params: Record<string, string> = {}
        if (sessionId) {
          params.session_id = sessionId
        }
        const response = await apiClient.get(API_ENDPOINTS.PROJECTS.TREE, { params })
        if (cancelled) return
        const raw = response.data
        const tree = raw?.children ?? []
        const flat = raw?.items ?? []
        setRemoteTreeData(tree.length > 0 ? tree : flat)
      } catch {
        // 静默失败，使用直接传入的数据
        if (!cancelled) {
          setRemoteTreeData([])
        }
      } finally {
        if (!cancelled) {
          setIsLoadingRemote(false)
        }
      }
    }

    loadTreeData()

    return () => {
      cancelled = true
    }
  }, [sessionId, rawProps.dataSource])

  /** 实际使用的树数据：优先使用远程数据，否则使用直接传入的数据 */
  const effectiveData = remoteTreeData.length > 0 ? remoteTreeData : allData

  /** 选中节点 ID */
  const [selectedId, setSelectedId] = useState<string | null>(null)
  /** 搜索关键词 */
  const [searchKeyword, setSearchKeyword] = useState('')
  /** 展开的节点 ID 集合 */
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => {
    return collectExpandedIds(allData, nodeChildrenField, expandLevel)
  })

  /** 过滤后的数据 */
  const filteredData = useMemo(() => {
    return filterNodes(effectiveData, searchKeyword, nodeTitleField, nodeChildrenField)
  }, [effectiveData, searchKeyword, nodeTitleField, nodeChildrenField])

  /**
   * 切换节点展开/折叠状态
   *
   * @param nodeId - 节点 ID
   */
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

  /**
   * 处理节点选中
   *
   * @param nodeId - 节点 ID
   */
  const handleSelect = useCallback((nodeId: string) => {
    setSelectedId(nodeId)
  }, [])

  /**
   * 处理搜索关键词变化
   *
   * @param e - 输入事件
   */
  const handleSearchChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setSearchKeyword(e.target.value)
  }, [])

  /** 空状态渲染 */
  if (effectiveData.length === 0 && !isLoadingRemote) {
    return (
      <div className="w-full rounded-lg border">
        {title && (
          <div className="border-b bg-muted/50 px-4 py-2">
            <h3 className="text-foreground text-sm font-semibold">{title}</h3>
          </div>
        )}
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
      {/* 标题栏 */}
      {title && (
        <div className="border-b bg-muted/50 px-4 py-2">
          <h3 className="text-foreground text-sm font-semibold">{title}</h3>
        </div>
      )}

      {/* 搜索框 */}
      {showSearch && (
        <div className="border-b px-3 py-2">
          <div className="relative">
            <Search className="text-muted-foreground absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2" />
            <input
              type="text"
              value={searchKeyword}
              onChange={handleSearchChange}
              placeholder="搜索节点..."
              className="bg-muted/50 focus:bg-background w-full rounded-md border py-1.5 pl-7 pr-3 text-xs outline-none transition-colors focus:border-status-info/50"
            />
          </div>
        </div>
      )}

      {/* 树节点列表 */}
      <div className="py-1">
        {filteredData.length === 0 ? (
          <div className="px-4 py-6 text-center">
            <p className="text-muted-foreground text-xs">未找到匹配的节点</p>
          </div>
        ) : (
          filteredData.map((node) => (
            <TreeNode
              key={node.id ?? String(node.title ?? Math.random())}
              node={node}
              depth={0}
              expandedIds={expandedIds}
              selectedId={selectedId}
              showStatus={showStatus}
              showProgress={showProgress}
              nodeIconField={nodeIconField}
              nodeTitleField={nodeTitleField}
              nodeStatusField={nodeStatusField}
              nodeChildrenField={nodeChildrenField}
              statusConfig={statusConfig}
              onToggle={handleToggle}
              onSelect={handleSelect}
              onNodeClick={onNodeClick}
            />
          ))
        )}
      </div>
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
  /** 节点点击回调（用于外部处理节点点击事件） */
  onNodeClick?: (node: TreeNodeData) => void
}

/**
 * 树节点组件
 *
 * 递归渲染单个树节点及其子节点，处理展开/折叠动画、
 * 状态图标、进度条和选中高亮。
 *
 * @param props - 节点组件属性
 * @returns 树节点渲染结果
 */
function TreeNode({
  node,
  depth,
  expandedIds,
  selectedId,
  showStatus,
  showProgress,
  nodeIconField,
  nodeTitleField,
  nodeStatusField,
  nodeChildrenField,
  statusConfig,
  onToggle,
  onSelect,
  onNodeClick,
}: TreeNodeProps): React.ReactNode {
  const nodeId = node.id ?? String(node.title ?? Math.random())
  const title = String(getNodeField(node, nodeTitleField) ?? '未命名')
  const icon = getNodeField(node, nodeIconField) as string | undefined
  const status = getNodeField(node, nodeStatusField) as string | undefined
  const children = getNodeField(node, nodeChildrenField) as TreeNodeData[] | undefined
  const progress = node.progress as number | undefined
  const description = node.description as string | undefined

  const hasChildren = Array.isArray(children) && children.length > 0
  const isExpanded = expandedIds.has(nodeId)
  const isSelected = selectedId === nodeId

  /**
   * 处理节点点击事件
   *
   * 如果提供了 onNodeClick 回调则调用它；
   * 同时执行选中逻辑和展开/折叠逻辑。
   */
  const handleClick = useCallback(() => {
    onSelect(nodeId)
    if (onNodeClick) {
      onNodeClick(node)
    }
    if (hasChildren) {
      onToggle(nodeId)
    }
  }, [nodeId, hasChildren, onToggle, onSelect, onNodeClick, node])

  /**
   * 阻止展开箭头的点击冒泡
   */
  const handleChevronClick = useCallback(
    (e: React.MouseEvent) => {
      e.stopPropagation()
      onToggle(nodeId)
    },
    [nodeId, onToggle],
  )

  /** 获取状态图标 */
  const statusInfo = showStatus && status ? getStatusIcon(status, statusConfig) : null

  /** 进度值处理 */
  const clampedProgress =
    typeof progress === 'number' ? Math.max(0, Math.min(100, progress)) : null

  return (
    <div>
      {/* 节点行 */}
      <div
        className={`group flex cursor-pointer items-center py-1.5 transition-colors hover:bg-accent ${
          isSelected
            ? 'bg-accent/50 border-l-2 border-l-status-info'
            : 'border-l-2 border-l-transparent'
        }`}
        style={{ paddingLeft: `${depth * 20 + 8}px` }}
        onClick={handleClick}
        title={description ?? title}
      >
        {/* 展开/折叠箭头 */}
        <button
          className={`mr-1 flex h-5 w-5 shrink-0 items-center justify-center rounded transition-transform ${
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

        {/* 节点图标 */}
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

        {/* 状态图标 */}
        {statusInfo && (
          <span className="mr-1.5 shrink-0" title={statusInfo.label}>
            {statusInfo.icon}
          </span>
        )}

        {/* 标题与描述 */}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className={`truncate text-sm ${isSelected ? 'text-foreground font-medium' : 'text-foreground/90'}`}>
              {title}
            </span>
            {statusInfo && (
              <span className={`shrink-0 text-[10px] ${statusInfo.color}`}>
                {statusInfo.label}
              </span>
            )}
          </div>

          {/* 进度条 */}
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

        {/* 子节点数量标记 */}
        {hasChildren && (
          <span className="text-muted-foreground/50 mr-2 shrink-0 text-[10px]">
            {children!.length}
          </span>
        )}
      </div>

      {/* 子节点递归渲染（带展开/折叠动画） */}
      {hasChildren && (
        <div
          className={`overflow-hidden transition-all duration-200 ease-in-out ${
            isExpanded ? 'max-h-[9999px] opacity-100' : 'max-h-0 opacity-0'
          }`}
        >
          {children!.map((child) => (
            <TreeNode
              key={child.id ?? String(child.title ?? Math.random())}
              node={child}
              depth={depth + 1}
              expandedIds={expandedIds}
              selectedId={selectedId}
              showStatus={showStatus}
              showProgress={showProgress}
              nodeIconField={nodeIconField}
              nodeTitleField={nodeTitleField}
              nodeStatusField={nodeStatusField}
              nodeChildrenField={nodeChildrenField}
              statusConfig={statusConfig}
              onToggle={onToggle}
              onSelect={onSelect}
              onNodeClick={onNodeClick}
            />
          ))}
        </div>
      )}
    </div>
  )
}
