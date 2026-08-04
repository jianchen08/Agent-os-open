/**
 * 类型定义统一导出
 */

// 导出模型类型
export type {
  ApprovalRequest,
  Message,
  MessageRole,
  RiskLevel,
  Session,
  User,
} from './models'

// 导出图类型
export type { Edge, GraphData, Node, NodeData, NodePosition, NodeStatus, NodeType } from './graph'

// 导出API类型
export type {
  ApiError,
  ApiResponse,
  AuthResponse,
  CreateSessionResponse,
  GetGraphResponse,
  GetMessagesResponse,
  GetSessionsResponse,
  LoginRequest,
  RegisterRequest,
  SendMessageRequest,
  SendMessageResponse,
  TokenResponse,
} from './api'

// 导出配置类型
export type {
  APIKeyConfig,
  LLMConfigResponse,
  LLMModel,
  LLMModelFormData,
  LLMProvider,
  ModelDefaultParams,
} from './config'

// 导出工具类型
export type {
  CodeEntry,
  Tool,
  ToolCategory,
  ToolDetail,
  ToolFormData,
  ToolListResponse,
} from './tool'

// 导出主题类型
export type {
  BackgroundConfig,
  BackgroundsConfig,
  ButtonConfig,
  CardConfig,
  ComponentsConfig,
  EffectsConfig,
  InputConfig,
  ThemeColors,
  ThemeConfig,
  ThemeInfo,
  ThemeMode,
} from './theme'

// 导出监控类型
export type {
  DiskUsage,
  MemoryUsage,
  MonitoringData,
  SystemMetrics,
  SystemMetricsResponse,
  TaskInfo,
  TaskListResponse,
  TaskStatistics,
  TaskStatisticsResponse,
} from './monitoring'

// 导出任务执行闭环系统类型
export type {
  // 基础类型
  ProjectStatus,
  TaskStatus,
  TaskType,
  TaskPhase,
  PhaseStatusType,
  ACStatus,
  EvaluatorType,
  AgentLevel,
  AgentTabStatus,
  // 核心类型
  PhaseResult,
  AcceptanceCriterion,
  Task,
  Project,
  AgentTab,
  // 消息类型
  TaskMessageType,
  TaskMessageData,
  // WebSocket 事件类型
  TaskWSEventType,
  TaskWSEvent,
  ProjectCreatedEvent,
  ProjectProgressEvent,
  ProjectPausedEvent,
  ProjectResumedEvent,
  TaskCreatedEvent,
  TaskPhaseChangedEvent,
  TaskACEvaluatedEvent,
  TaskCompletedEvent,
  TaskFailedEvent,
  AutoExecuteTriggeredEvent,
  // API 类型
  CreateProjectRequest,
  CreateProjectResponse,
  GetProjectsResponse,
  GetProjectResponse,
  ToggleAutoExecuteRequest,
  ToggleAutoExecuteResponse,
  PauseProjectResponse,
  ResumeProjectResponse,
  GetTaskPhaseResponse,
  CompletePreparePhaseRequest,
  CompleteExecutePhaseRequest,
  GetPhaseOutputResponse,
  GetTaskACsResponse,
  EvaluateACRequest,
  EvaluateACResponse,
  GetACResultResponse,
  // UI 相关类型
  TaskCardStyle,
  TaskPanelState,
  TaskFilter,
  TaskSortBy,
  TaskSortOrder,
  TaskListQuery,
} from './task'

// 导出活动卡片类型（统一的活动展示组件）
export type {
  ActivityType,
  ActivityStatus,
  ActivityData,
  ActivityDetailBlock,
  ActivityAction,
  ActivityCardProps,
} from './activity'

// 导出统一消息 Part 类型
export type {
  MessagePart,
  TextPart,
  ThinkingPart,
  ToolCallPart,
  SystemPart,
  PartState,
  ToolCallPartState,
  SystemLevel,
} from './messageParts'
