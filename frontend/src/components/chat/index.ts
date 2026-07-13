/**
 * 消息系统组件导出
 */

export { ChatContainer } from './ChatContainer'
export { ChatInput } from './ChatInput'
export { MessageItem } from './MessageItem'
export { MessageList } from './MessageList'

// Agent Tab 组件
export { AgentTabBar } from './AgentTabBar'
export type { AgentTab, AgentTabBarProps } from './AgentTabBar'

// 分层 Agent 系统组件
export { AgentTabItem } from './AgentTabItem'
export type { AgentTabItemProps, AgentTabItemData, AgentTabStatus } from './AgentTabItem'

export { SubAgentCard } from './SubAgentCard'
export type {
  SubAgentCardProps,
  SubAgentData,
  SubAgentDisplayMode,
  SubAgentStatus,
} from './SubAgentCard'

// 通知系统组件
export { NotificationCenter } from './NotificationCenter'
export type { NotificationCenterProps } from './NotificationCenter'
export { NotificationItemComponent } from './NotificationItem'
export type { NotificationItemProps } from './NotificationItem'

// 子 Tab 路由增强
export { SubTabRouter, getSubTabRouterApi } from './SubTabRouter'
export type { SubTabRouterProps } from './SubTabRouter'

// 投票面板组件
export { VotingPanel } from './VotingPanel'
export type { VotingPanelProps } from './VotingPanel'

// 文件上传组件
export { FileUploadZone } from './FileUploadZone'
export type { FileUploadZoneProps, UploadableFile } from './FileUploadZone'

// Markdown 渲染组件
export {
  CodeBlock,
  MarkdownRenderer,
  MermaidDiagram,
  type CodeBlockProps,
  type MarkdownRendererProps,
  type MermaidDiagramProps,
} from './markdown'

// 类型导出
export type {
  Attachment,
  ChatContainerProps,
  ChatInputMode,
  ChatInputProps,
  ChatInputState,
  ExecutionState,
  FileUploadStatus,
  MessageContentType,
  MessageItemProps,
  MessageListProps,
  PendingFile,
  SendMessageParams,
} from './types'
