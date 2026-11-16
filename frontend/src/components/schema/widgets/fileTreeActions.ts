/**
 * 通用树节点域动作注册缝——file_tree widget 不绑定任何后端域。
 *
 * 节点启停（开关动作）、状态词表（图标配置/筛选选项/归一化/活跃集合/默认
 * 筛选）与容器节点判定属数据源域知识，由域绑定经注册注入（组合根挂载
 * 副作用注册，如任务域 taskFileTreeActions）。未注册绑定时组件按通用形态
 * 退化：开关隐藏、筛选只余「全部」、状态图标走通用兜底（CircleDot）——
 * workspace:// 文件树等纯展示场景零域依赖。
 */

/** 状态显示配置项（与 TreeWidgetConfig.statusConfig 同构） */
export interface FileTreeStatusConfigItem {
  /** 图标名称 */
  icon: string
  /** 颜色类名 */
  color: string
  /** 状态标签 */
  label: string
}

/** 树节点数据结构（FileTreeWidget 通用树形数据契约） */
export interface TreeNodeData {
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

/** 树形组件配置属性（file_tree widget 外部配置面） */
export interface TreeWidgetConfig {
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
  statusConfig?: Record<string, FileTreeStatusConfigItem>
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
  /** 目录/链接行点击回调（目录选择场景追踪当前目录；文件行不触发） */
  onDirectoryClick?: (node: TreeNodeData) => void
  /** 会话 ID（用于按会话过滤/加载任务数据） */
  sessionId?: string
}

/** 组件可接触的树节点最小结构（FileTreeWidget 的 TreeNodeData 结构兼容） */
export interface FileTreeNodeRef {
  id?: string
  status?: string
  [key: string]: unknown
}

/** 一个数据源域向 file_tree 注入的动作与词表 */
export interface FileTreeDomainBinding {
  /** 域 id（诊断用） */
  id: string
  /** 状态词表 */
  statuses: {
    /** 状态显示配置（键 = 归一化状态） */
    config: Record<string, FileTreeStatusConfigItem>
    /** 筛选选项（域状态全集；「全部」空值选项由组件自带首位） */
    filterOptions: ReadonlyArray<{ value: string; label: string }>
    /** 状态键归一化（词表折叠比较用） */
    normalize: (status: string) => string
    /** 活跃态集合（「仅活跃」筛选与开关受控值同源） */
    active: ReadonlySet<string>
    /** 默认筛选值（无显式 defaultStatusFilter 配置时；空串 = 全部） */
    defaultFilter: string
  }
  /** 启停切换动作（组件负责级联后代遍历，本动作按单节点 id 调用） */
  toggleEnabled: (nodeId: string, enabled: boolean) => Promise<void>
  /** 开关受控值：节点当前是否启用（活跃） */
  isEnabled: (status: unknown) => boolean
  /** 容器节点判定（容器无会话入口，如任务树 task_scope=container） */
  isContainerNode?: (node: FileTreeNodeRef) => boolean
}

let binding: FileTreeDomainBinding | null = null

/** 注册域绑定（后注册覆盖；当前单绑定模型——多域并存需求出现时再泛化） */
export function registerFileTreeDomainBinding(next: FileTreeDomainBinding): void {
  binding = next
}

/** 当前域绑定（未注册返回 null，组件按通用形态退化） */
export function getFileTreeDomainBinding(): FileTreeDomainBinding | null {
  return binding
}
