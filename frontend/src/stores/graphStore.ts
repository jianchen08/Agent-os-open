/**
 * 执行图状态管理Store
 *
 * 使用真实后端API和WebSocket事件更新执行图状态。
 * 支持多个 Agent 并行执行时的多图管理。
 *
 * Requirements: 5.1, 5.2, 3.2
 */

import { create } from 'zustand'
import { WS_SERVER_EVENTS } from '@/constants/websocket'
import * as graphApi from '@/services/api/graph'
import { reportError, ErrorType } from '@/services/errorReporting'
import { webSocketService } from '@/services/websocket/WebSocketService'
import { mapNodeStatus, mapBackendGraphToGraphData, type BackendGraphData } from '@/utils/mappers'
import type { GraphData, Node, NodeStatus } from '@/types/graph'

/**
 * 状态变更事件数据
 */
interface StateChangeEventData {
  /** 线程ID */
  thread_id?: string
  /** 执行ID */
  execution_id?: string
  /** 之前的状态 */
  previous_state?: string
  /** 当前状态 */
  current_state?: string
  /** 节点ID（如果是节点状态变更） */
  node_id?: string
  /** 节点状态 */
  node_status?: string
  /** 执行图数据（可选，某些事件会包含完整图数据） */
  graph?: BackendGraphData
}

/**
 * 任务完成事件数据
 */
interface TaskCompletedEventData {
  /** 线程ID */
  thread_id?: string
  /** 执行ID */
  execution_id?: string
  /** 节点ID */
  node_id?: string
  /** 输出数据 */
  output?: unknown
}

/**
 * 工作流步骤更新事件数据
 * Requirements: 3.2
 */
interface WorkflowStepUpdateEventData {
  /** 执行ID */
  execution_id?: string
  /** 步骤ID */
  step_id?: string
  /** 步骤名称 */
  step_name?: string
  /** 步骤状态 */
  status?: string
  /** 步骤输出 */
  output?: unknown
  /** 线程ID */
  thread_id?: string
}

/**
 * 单个执行图的状态
 */
interface ExecutionGraphState {
  /** 执行ID */
  executionId: string
  /** 执行图数据 */
  graphData: GraphData
  /** 执行状态 */
  status: 'running' | 'completed' | 'failed'
  /** 创建时间 */
  createdAt: number
}

/**
 * 执行图状态接口
 *
 * 支持多图管理：
 * - graphsByExecution: 按 execution_id 分组的执行图
 * - activeExecutionId: 当前激活的执行ID
 * - graphData: 当前激活执行的图数据（兼容旧接口）
 */
interface GraphState {
  /** 按执行ID分组的执行图 - 支持多 Agent 并行 */
  graphsByExecution: Record<string, ExecutionGraphState>
  /** 当前激活的执行ID */
  activeExecutionId: string | null
  /** 执行图数据（当前激活执行的图，兼容旧接口） */
  graphData: GraphData | null
  /** 当前选中的节点 */
  selectedNode: Node | null
  /** 当前线程ID */
  currentThreadId: string | null
  /** 是否正在加载 */
  isLoading: boolean
  /** 错误信息 */
  error: string | null
  /** 从后端获取执行图 */
  fetchGraph: (sessionId: string) => Promise<void>
  /** 设置执行图数据 */
  setGraphData: (data: GraphData) => void
  /** 设置指定执行的图数据 */
  setExecutionGraphData: (executionId: string, data: GraphData) => void
  /** 设置当前激活的执行 */
  setActiveExecution: (executionId: string | null) => void
  /** 获取指定执行的图数据 */
  getExecutionGraph: (executionId: string) => GraphData | null
  /** 获取所有执行图列表 */
  getAllExecutionGraphs: () => ExecutionGraphState[]
  /** 更新节点状态 */
  updateNodeStatus: (nodeId: string, status: NodeStatus, executionId?: string) => void
  /** 更新节点数据 */
  updateNodeData: (nodeId: string, data: Partial<Node['data']>, executionId?: string) => void
  /** 选择节点 */
  selectNode: (nodeId: string) => void
  /** 清除选中的节点 */
  clearSelectedNode: () => void
  /** 初始化WebSocket事件监听 */
  initWebSocketListeners: () => void
  /** 清理WebSocket事件监听 */
  cleanupWebSocketListeners: () => void
  /** 处理状态变更事件 */
  handleStateChange: (data: StateChangeEventData) => void
  /** 处理任务完成事件 */
  handleTaskCompleted: (data: TaskCompletedEventData) => void
  /** 处理工作流步骤更新事件 - Requirements: 3.2 */
  handleWorkflowStepUpdate: (data: WorkflowStepUpdateEventData) => void
  /** 清除错误 */
  clearError: () => void
  /** 清除图数据 */
  clearGraph: () => void
  /** 清除指定执行的图数据 */
  clearExecutionGraph: (executionId: string) => void
  /** 清除所有执行图 */
  clearAllExecutionGraphs: () => void
}

/**
 * 执行图Store
 *
 * 使用真实后端API和WebSocket事件更新执行图状态。
 * 支持多个 Agent 并行执行时的多图管理。
 *
 * Requirements: 5.1, 5.2, 3.2
 */
export const useGraphStore = create<GraphState>((set, get) => ({
  graphsByExecution: {},
  activeExecutionId: null,
  graphData: null,
  selectedNode: null,
  currentThreadId: null,
  isLoading: false,
  error: null,

  /**
   * 从后端获取执行图
   *
   * 调用后端线程详情端点获取执行图数据。
   *
   * Requirements: 5.1
   */
  fetchGraph: async (sessionId: string) => {
    set({ isLoading: true, error: null, currentThreadId: sessionId })

    try {
      // 调用真实API获取执行图
      // Requirements: 5.1
      const graphData = await graphApi.getGraph(sessionId)

      set({
        graphData,
        isLoading: false,
        error: null,
        // 清除之前选中的节点
        selectedNode: null,
      })
    } catch (error: any) {
      const errorMessage = error.message || '获取执行图失败'
      reportError(errorMessage, ErrorType.SERVER, undefined, {
        componentName: 'GraphStore',
        operation: 'fetchGraph',
        sessionId,
      })
      set({ isLoading: false, error: errorMessage })
      throw new Error(errorMessage)
    }
  },

  /**
   * 设置执行图数据（兼容旧接口）
   */
  setGraphData: (data: GraphData) => {
    set({
      graphData: data,
      // 清除之前选中的节点，因为新的图数据可能不包含该节点
      selectedNode: null,
    })
  },

  /**
   * 设置指定执行的图数据
   *
   * 用于多 Agent 并行执行时管理各自的执行图。
   */
  setExecutionGraphData: (executionId: string, data: GraphData) => {
    const { graphsByExecution, activeExecutionId } = get()

    const existingState = graphsByExecution[executionId]
    const newState: ExecutionGraphState = {
      executionId,
      graphData: data,
      status: existingState?.status || 'running',
      createdAt: existingState?.createdAt || Date.now(),
    }

    const newGraphsByExecution = {
      ...graphsByExecution,
      [executionId]: newState,
    }

    // 如果是当前激活的执行，同步更新 graphData
    const newGraphData = activeExecutionId === executionId ? data : get().graphData

    set({
      graphsByExecution: newGraphsByExecution,
      graphData: newGraphData,
    })
  },

  /**
   * 设置当前激活的执行
   *
   * 切换显示的执行图。
   */
  setActiveExecution: (executionId: string | null) => {
    const { graphsByExecution } = get()

    const graphData = executionId ? graphsByExecution[executionId]?.graphData || null : null

    set({
      activeExecutionId: executionId,
      graphData,
      selectedNode: null,
    })
  },

  /**
   * 获取指定执行的图数据
   */
  getExecutionGraph: (executionId: string) => {
    return get().graphsByExecution[executionId]?.graphData || null
  },

  /**
   * 获取所有执行图列表
   *
   * 按创建时间倒序排列。
   */
  getAllExecutionGraphs: () => {
    const { graphsByExecution } = get()
    return Object.values(graphsByExecution).sort((a, b) => b.createdAt - a.createdAt)
  },

  /**
   * 更新节点状态
   *
   * 根据WebSocket事件更新节点状态。
   * 支持指定 executionId 更新特定执行图。
   *
   * Requirements: 5.2
   */
  updateNodeStatus: (nodeId: string, status: NodeStatus, executionId?: string) => {
    const { graphData, selectedNode, graphsByExecution, activeExecutionId } = get()

    // 如果指定了 executionId，更新对应执行图
    if (executionId && graphsByExecution[executionId]) {
      const execState = graphsByExecution[executionId]
      const updatedNodes = execState.graphData.nodes.map((node) => {
        if (node.id === nodeId) {
          return { ...node, data: { ...node.data, status } }
        }
        return node
      })

      const newGraphData = { ...execState.graphData, nodes: updatedNodes }
      const newGraphsByExecution = {
        ...graphsByExecution,
        [executionId]: { ...execState, graphData: newGraphData },
      }

      // 如果是当前激活的执行，同步更新 graphData
      const syncGraphData = activeExecutionId === executionId ? newGraphData : graphData
      const newSelectedNode =
        selectedNode?.id === nodeId
          ? updatedNodes.find((node) => node.id === nodeId) || null
          : selectedNode

      set({
        graphsByExecution: newGraphsByExecution,
        graphData: syncGraphData,
        selectedNode: newSelectedNode,
      })
      return
    }

    // 兼容旧逻辑：更新当前 graphData
    if (!graphData) {
      return
    }

    const updatedNodes = graphData.nodes.map((node) => {
      if (node.id === nodeId) {
        return { ...node, data: { ...node.data, status } }
      }
      return node
    })

    const newGraphData = { ...graphData, nodes: updatedNodes }
    const newSelectedNode =
      selectedNode?.id === nodeId
        ? updatedNodes.find((node) => node.id === nodeId) || null
        : selectedNode

    set({
      graphData: newGraphData,
      selectedNode: newSelectedNode,
    })
  },

  /**
   * 更新节点数据
   *
   * 支持指定 executionId 更新特定执行图。
   */
  updateNodeData: (nodeId: string, data: Partial<Node['data']>, executionId?: string) => {
    const { graphData, selectedNode, graphsByExecution, activeExecutionId } = get()

    // 如果指定了 executionId，更新对应执行图
    if (executionId && graphsByExecution[executionId]) {
      const execState = graphsByExecution[executionId]
      const updatedNodes = execState.graphData.nodes.map((node) => {
        if (node.id === nodeId) {
          return { ...node, data: { ...node.data, ...data } }
        }
        return node
      })

      const newGraphData = { ...execState.graphData, nodes: updatedNodes }
      const newGraphsByExecution = {
        ...graphsByExecution,
        [executionId]: { ...execState, graphData: newGraphData },
      }

      const syncGraphData = activeExecutionId === executionId ? newGraphData : graphData
      const newSelectedNode =
        selectedNode?.id === nodeId
          ? updatedNodes.find((node) => node.id === nodeId) || null
          : selectedNode

      set({
        graphsByExecution: newGraphsByExecution,
        graphData: syncGraphData,
        selectedNode: newSelectedNode,
      })
      return
    }

    // 兼容旧逻辑
    if (!graphData) {
      return
    }

    const updatedNodes = graphData.nodes.map((node) => {
      if (node.id === nodeId) {
        return { ...node, data: { ...node.data, ...data } }
      }
      return node
    })

    const newGraphData = { ...graphData, nodes: updatedNodes }
    const newSelectedNode =
      selectedNode?.id === nodeId
        ? updatedNodes.find((node) => node.id === nodeId) || null
        : selectedNode

    set({
      graphData: newGraphData,
      selectedNode: newSelectedNode,
    })
  },

  /**
   * 选择节点
   */
  selectNode: (nodeId: string) => {
    const { graphData } = get()

    if (!graphData) {
      return
    }

    const node = graphData.nodes.find((n) => n.id === nodeId)

    if (node) {
      set({ selectedNode: node })
    }
  },

  /**
   * 清除选中的节点
   */
  clearSelectedNode: () => {
    set({ selectedNode: null })
  },

  /**
   * 初始化WebSocket事件监听
   *
   * 订阅WebSocket状态变更事件，实时更新执行图。
   *
   * Requirements: 5.2
   */
  initWebSocketListeners: () => {
    const { handleStateChange, handleTaskCompleted, handleWorkflowStepUpdate } = get()

    // 订阅状态变更事件
    // Requirements: 4.3, 5.2
    webSocketService.subscribe(
      WS_SERVER_EVENTS.STATE_CHANGE,
      handleStateChange as (data: unknown) => void,
    )

    // 订阅任务完成事件
    // Requirements: 4.5
    webSocketService.subscribe(
      WS_SERVER_EVENTS.TASK_COMPLETED,
      handleTaskCompleted as (data: unknown) => void,
    )

    // 订阅工作流步骤更新事件
    // Requirements: 3.2
    webSocketService.subscribe(
      WS_SERVER_EVENTS.WORKFLOW_STEP_UPDATE,
      handleWorkflowStepUpdate as (data: unknown) => void,
    )
  },

  /**
   * 清理WebSocket事件监听
   */
  cleanupWebSocketListeners: () => {
    const { handleStateChange, handleTaskCompleted, handleWorkflowStepUpdate } = get()

    webSocketService.unsubscribe(
      WS_SERVER_EVENTS.STATE_CHANGE,
      handleStateChange as (data: unknown) => void,
    )
    webSocketService.unsubscribe(
      WS_SERVER_EVENTS.TASK_COMPLETED,
      handleTaskCompleted as (data: unknown) => void,
    )
    webSocketService.unsubscribe(
      WS_SERVER_EVENTS.WORKFLOW_STEP_UPDATE,
      handleWorkflowStepUpdate as (data: unknown) => void,
    )
  },

  /**
   * 处理状态变更事件
   *
   * 根据WebSocket state_change事件更新执行图节点状态。
   *
   * Requirements: 4.3, 5.2
   */
  handleStateChange: (data: StateChangeEventData) => {
    const { currentThreadId, updateNodeStatus, setGraphData } = get()

    // 检查是否是当前线程的事件
    if (data.thread_id && data.thread_id !== currentThreadId) {
      return
    }

    // 如果事件包含完整的图数据，直接更新
    if (data.graph) {
      const graphData = mapBackendGraphToGraphData(data.graph)
      setGraphData(graphData)
      return
    }

    // 如果是节点状态变更
    if (data.node_id && data.node_status) {
      const status = mapNodeStatus(data.node_status)
      updateNodeStatus(data.node_id, status)
    }
  },

  /**
   * 处理任务完成事件
   *
   * 根据WebSocket task_completed事件更新节点状态为completed。
   *
   * Requirements: 4.5, 5.2
   */
  handleTaskCompleted: (data: TaskCompletedEventData) => {
    const { currentThreadId, updateNodeStatus, updateNodeData } = get()

    // 检查是否是当前线程的事件
    if (data.thread_id && data.thread_id !== currentThreadId) {
      return
    }

    // 更新节点状态为completed
    if (data.node_id) {
      updateNodeStatus(data.node_id, 'completed')

      // 如果有输出数据，也更新节点数据
      if (data.output !== undefined) {
        updateNodeData(data.node_id, { output: data.output })
      }
    }
  },

  /**
   * 处理工作流步骤更新事件
   *
   * 根据WebSocket workflow_step_update事件更新执行图节点。
   * 将工作流步骤映射为执行图中的节点。
   * 支持多 Agent 并行执行，按 execution_id 分组管理。
   *
   * 工作流显示逻辑：
   * - 主 Agent 作为根节点
   * - 工具调用按顺序串联（前一个工具 → 后一个工具）
   * - 调用复合 Agent 时，创建复合 Agent 节点，主 Agent → 复合 Agent
   * - 复合 Agent 内部步骤连接到复合 Agent 节点（复合 Agent → 子步骤）
   *
   * Requirements: 3.2
   */
  handleWorkflowStepUpdate: (data: WorkflowStepUpdateEventData) => {
    const {
      graphsByExecution,
      activeExecutionId,
      setExecutionGraphData,
      setGraphData,
      graphData: currentGraphData,
    } = get()

    const executionId = data.execution_id
    const stepId = data.step_id
    const stepName = data.step_name || '未知步骤'
    const status = data.status || 'pending'
    const agentName = (data as any).agent_name // 复合 Agent 会传递 agent_name
    const agentType = (data as any).agent_type // 复合 Agent 会传递 agent_type
    const parentAgentId = (data as any).parent_agent_id // 父 Agent ID（复合 Agent 内部步骤会有）

    if (!stepId) {
      return
    }

    // 映射后端状态到前端节点状态
    const nodeStatus = mapNodeStatus(status)

    // 检测是否为 Agent 调用（call_agent 工具）
    const isAgentCall = stepName === 'call_agent' || stepName.startsWith('call_agent:')
    // 检测是否为复合 Agent 的内部步骤（有 parent_agent_id 或 agent_type 为 composite）
    const isCompositeInternalStep = !!parentAgentId || agentType === 'composite'
    // 从步骤名称中提取子 Agent 名称（如果有）
    const subAgentName =
      isAgentCall && stepName.includes(':')
        ? stepName.split(':')[1]
        : isAgentCall
          ? '子 Agent'
          : agentName || null

    // 辅助函数：创建或更新图数据
    const processGraphUpdate = (
      existingGraphData: GraphData,
      execId?: string,
    ): GraphData | null => {
      const existingNode = existingGraphData.nodes.find((n) => n.id === stepId)

      if (existingNode) {
        // 更新现有节点状态
        const updatedNodes = existingGraphData.nodes.map((node) => {
          if (node.id === stepId) {
            return {
              ...node,
              data: {
                ...node.data,
                status: nodeStatus,
                output: data.output !== undefined ? data.output : node.data.output,
              },
            }
          }
          return node
        })

        // 如果是 Agent 调用完成，检查是否需要更新主 Agent 状态
        if (
          (isAgentCall || isCompositeInternalStep) &&
          (nodeStatus === 'completed' || nodeStatus === 'failed')
        ) {
          const mainAgentNode = updatedNodes.find((n) => n.data.isMainAgent)
          if (mainAgentNode) {
            const allChildrenDone = updatedNodes
              .filter((n) => !n.data.isMainAgent && n.data.parentId === mainAgentNode.id)
              .every((n) => n.data.status === 'completed' || n.data.status === 'failed')
            if (allChildrenDone) {
              const mainIdx = updatedNodes.findIndex((n) => n.id === mainAgentNode.id)
              if (mainIdx >= 0) {
                updatedNodes[mainIdx] = {
                  ...updatedNodes[mainIdx],
                  data: { ...updatedNodes[mainIdx].data, status: 'completed' },
                }
              }
            }
          }
        }

        return { ...existingGraphData, nodes: updatedNodes }
      }

      // 创建新节点
      const currentNodes = [...existingGraphData.nodes]
      const currentEdges = [...existingGraphData.edges]

      // 主 Agent 节点 ID
      const mainAgentId = execId ? `main-agent-${execId}` : 'main-agent-default'

      // 如果是第一个节点，先创建主 Agent 节点
      if (currentNodes.length === 0) {
        const mainAgentNode: Node = {
          id: mainAgentId,
          type: 'agent',
          data: {
            label: '主 Agent',
            status: 'running',
            isMainAgent: true,
            agentName: '主 Agent',
          },
          position: { x: 0, y: 0 },
        }
        currentNodes.push(mainAgentNode)
      }

      // 查找主 Agent 节点
      const mainAgentNode = currentNodes.find((n) => n.data.isMainAgent)

      // 跳过 workflow_start 和 workflow_complete 等元事件，只更新主节点状态
      if (
        stepName === 'workflow_start' ||
        stepName === 'workflow_complete' ||
        stepName === 'workflow_failed'
      ) {
        if (mainAgentNode) {
          const newStatus =
            stepName === 'workflow_complete'
              ? 'completed'
              : stepName === 'workflow_failed'
                ? 'failed'
                : 'running'
          const mainIdx = currentNodes.findIndex((n) => n.id === mainAgentNode.id)
          if (mainIdx >= 0) {
            currentNodes[mainIdx] = {
              ...currentNodes[mainIdx],
              data: {
                ...currentNodes[mainIdx].data,
                status: newStatus as NodeStatus,
              },
            }
          }
        }
        return { nodes: currentNodes, edges: currentEdges }
      }

      // 确定父节点：
      // 1. 如果是复合 Agent 内部步骤，连接到复合 Agent 或上一个内部步骤（有依赖）
      // 2. 主 Agent 的工具调用都直接连接到主 Agent（并列，无依赖）
      let parentNodeId = mainAgentId

      if (isCompositeInternalStep && parentAgentId) {
        // 复合 Agent 内部步骤，有数据流依赖
        // 找到同一复合 Agent 下的上一个步骤
        const siblingSteps = currentNodes.filter(
          (n) => n.data.parentId === parentAgentId && n.id !== stepId,
        )
        if (siblingSteps.length > 0) {
          // 连接到上一个步骤（串联，表示依赖关系）
          parentNodeId = siblingSteps[siblingSteps.length - 1].id
        } else {
          // 第一个步骤，连接到复合 Agent
          parentNodeId = parentAgentId
        }
      }
      // 主 Agent 的工具调用：parentNodeId 保持为 mainAgentId（并列）

      // 创建新节点
      const newNode: Node = isAgentCall
        ? {
            // 子 Agent / 复合 Agent 节点
            id: stepId,
            type: 'agent',
            data: {
              label: subAgentName || stepName,
              status: nodeStatus,
              isMainAgent: false,
              agentName: subAgentName || stepName,
              output: data.output,
              parentId: mainAgentId,
            },
            position: { x: 0, y: 0 }, // 位置由 dagre 计算
          }
        : {
            // 普通工具节点
            id: stepId,
            type: 'tool',
            data: {
              label: stepName,
              status: nodeStatus,
              output: data.output,
              parentId: isCompositeInternalStep ? parentAgentId : mainAgentId,
            },
            position: { x: 0, y: 0 }, // 位置由 dagre 计算
          }

      currentNodes.push(newNode)

      // 创建边
      // 对于 Agent 调用，从主 Agent 连接
      // 对于普通工具，从父节点（主 Agent 或上一个工具）连接
      currentEdges.push({
        id: `edge-${parentNodeId}-${stepId}`,
        source: parentNodeId,
        target: stepId,
        label: isAgentCall ? '调用' : undefined,
      })

      return { nodes: currentNodes, edges: currentEdges }
    }

    // 如果有 execution_id，使用多图管理
    if (executionId) {
      const execState = graphsByExecution[executionId]
      const existingGraphData = execState?.graphData || { nodes: [], edges: [] }

      const newGraphData = processGraphUpdate(existingGraphData, executionId)
      if (newGraphData) {
        setExecutionGraphData(executionId, newGraphData)

        // 如果没有激活的执行，自动激活第一个
        if (!activeExecutionId) {
          get().setActiveExecution(executionId)
        }
      }
      return
    }

    // 兼容旧逻辑：无 execution_id 时使用单图模式
    const existingGraphData = currentGraphData || { nodes: [], edges: [] }
    const newGraphData = processGraphUpdate(existingGraphData)
    if (newGraphData) {
      setGraphData(newGraphData)
    }
  },

  /**
   * 清除错误
   */
  clearError: () => {
    set({ error: null })
  },

  /**
   * 清除图数据（兼容旧接口）
   */
  clearGraph: () => {
    set({
      graphData: null,
      selectedNode: null,
      currentThreadId: null,
      error: null,
    })
  },

  /**
   * 清除指定执行的图数据
   */
  clearExecutionGraph: (executionId: string) => {
    const { graphsByExecution, activeExecutionId } = get()

    const newGraphsByExecution = { ...graphsByExecution }
    delete newGraphsByExecution[executionId]

    // 如果清除的是当前激活的执行，重置激活状态
    const newActiveExecutionId = activeExecutionId === executionId ? null : activeExecutionId
    const newGraphData = newActiveExecutionId
      ? newGraphsByExecution[newActiveExecutionId]?.graphData || null
      : null

    set({
      graphsByExecution: newGraphsByExecution,
      activeExecutionId: newActiveExecutionId,
      graphData: newGraphData,
      selectedNode: null,
    })
  },

  /**
   * 清除所有执行图
   */
  clearAllExecutionGraphs: () => {
    set({
      graphsByExecution: {},
      activeExecutionId: null,
      graphData: null,
      selectedNode: null,
    })
  },
}))
