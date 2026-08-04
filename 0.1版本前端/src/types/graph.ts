/**
 * 执行图相关类型定义
 */

/**
 * 节点类型
 * - task: 任务节点
 * - tool: 工具调用节点
 * - decision: 决策节点
 * - agent: Agent 节点（主 Agent 或子 Agent）
 */
export type NodeType = 'task' | 'tool' | 'decision' | 'agent'

/**
 * 节点状态
 */
export type NodeStatus = 'pending' | 'running' | 'completed' | 'failed'

/**
 * 节点位置
 */
export interface NodePosition {
  /** X坐标 */
  x: number
  /** Y坐标 */
  y: number
}

/**
 * 节点数据
 * 添加索引签名以兼容 React Flow 的 Record<string, unknown> 要求
 */
export interface NodeData extends Record<string, unknown> {
  /** 节点标签 */
  label: string
  /** 节点状态 */
  status: NodeStatus
  /** 节点描述（可选） */
  description?: string
  /** 输入数据（可选） */
  input?: any
  /** 输出数据（可选） */
  output?: any
  /** 执行日志（可选） */
  logs?: string[]
  /** 是否为主 Agent（仅 agent 类型节点） */
  isMainAgent?: boolean
  /** Agent 名称（仅 agent 类型节点） */
  agentName?: string
  /** 父节点 ID（用于子 Agent 关联到主 Agent） */
  parentId?: string
  /** 错误信息（可选） */
  error?: string
  /** 开始时间（可选） */
  startTime?: string
  /** 结束时间（可选） */
  endTime?: string
  /** 执行耗时（毫秒，可选） */
  duration?: number

  // ============================================
  // 任务关联字段（参考文档第 7.7.3 节）
  // ============================================

  /** 关联的任务 ID */
  taskId?: string
  /** 关联的任务标题 */
  taskTitle?: string
  /** 任务类型（planning/execution/final_evaluation） */
  taskType?: 'planning' | 'execution' | 'final_evaluation'
  /** 当前任务阶段（prepare/execute/evaluate） */
  taskPhase?: 'prepare' | 'execute' | 'evaluate'
  /** AC 进度（如 "1/3"） */
  acProgress?: string
  /** AC 总数 */
  acTotal?: number
  /** AC 已通过数 */
  acPassed?: number
  /** Agent 层级（1/2/3） */
  agentLevel?: 1 | 2 | 3
  /** 子任务数量 */
  taskCount?: number

  // ============================================
  // UWF 工作流可视化字段
  // ============================================

  /** UWF 原始节点类型（用于工作流可视化） */
  uwfType?: string
  /** UWF 节点配置 */
  uwfConfig?: Record<string, unknown>
  /** 节点图标 */
  icon?: string
  /** 节点类型显示名称 */
  typeLabel?: string
}

/**
 * 节点类型
 */
export interface Node {
  /** 节点唯一标识 */
  id: string
  /** 节点类型 */
  type: NodeType
  /** 节点数据 */
  data: NodeData
  /** 节点位置 */
  position: NodePosition
}

/**
 * 边类型
 */
export interface Edge {
  /** 边唯一标识 */
  id: string
  /** 源节点ID */
  source: string
  /** 目标节点ID */
  target: string
  /** 边标签（可选） */
  label?: string
}

/**
 * 执行图数据
 */
export interface GraphData {
  /** 节点列表 */
  nodes: Node[]
  /** 边列表 */
  edges: Edge[]
}
