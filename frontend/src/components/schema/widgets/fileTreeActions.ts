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
