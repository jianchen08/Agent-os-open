/** 组件注册初始化 将所有已实现的组件注册到两套 Widget Registry */

import { ArtifactPreviewWidget } from '@/components/schema/widgets/ArtifactPreviewWidget'
import { ChartWidget } from '@/components/schema/widgets/ChartWidget'
import { CodeBlockWidget } from '@/components/schema/widgets/CodeBlockWidget'
import { CostDashboardWidget } from '@/components/schema/widgets/CostDashboardWidget'
import {
  AgentsPanel,
  MemoryPanel,
  MonitoringPanel,
  PluginsPanel,
  SettingsHubPanel,
  ToolsPanel,
  WorkspaceExplorerPanel,
} from '@/components/schema/widgets/PanelHostWidget'
import { SettingsHubWidget } from '@/components/schema/widgets/SettingsHubWidget'
import { DecisionWidget } from '@/components/schema/widgets/DecisionWidget'
import { EditorWidget } from '@/components/schema/widgets/EditorWidget'
import { FileTreeWidget } from '@/components/schema/widgets/FileTreeWidget'
import { FormWidget } from '@/components/schema/widgets/FormWidget'
import { GalleryWidget } from '@/components/schema/widgets/GalleryWidget'
import { HtmlPreviewWidget } from '@/components/schema/widgets/HtmlPreviewWidget'
import { KanbanWidget } from '@/components/schema/widgets/KanbanWidget'
import { ProgressWidget } from '@/components/schema/widgets/ProgressWidget'
import { ReviewDocumentWidget } from '@/components/schema/widgets/ReviewDocumentWidget'
import { StatusCardWidget } from '@/components/schema/widgets/StatusCardWidget'
import { TableWidget } from '@/components/schema/widgets/TableWidget'
import { TaskCardWidget } from '@/components/schema/widgets/TaskCardWidget'
import { TerminalWidget } from '@/components/schema/widgets/TerminalWidget'
import { WebviewWidget } from '@/components/schema/widgets/WebviewWidget'
import { widgetRegistry as composerRegistry } from './composer'
import { widgetRegistry } from './WidgetRegistry'
import type { WidgetComponent } from './WidgetRegistry'

/** Widget 注册条目 */
interface WidgetEntry {
  name: string
  component: React.ComponentType<Record<string, unknown>>
  spaces: string[]
  fallback?: string
}

/** 所有预置 Widget */
const WIDGETS: WidgetEntry[] = [
  { name: 'form', component: FormWidget, spaces: ['chat', 'workspace'] },
  { name: 'chart', component: ChartWidget, spaces: ['chat', 'workspace', 'floating'] },
  { name: 'gallery', component: GalleryWidget, spaces: ['chat', 'workspace', 'floating'] },
  { name: 'table', component: TableWidget, spaces: ['chat', 'workspace'], fallback: 'status_card' },
  { name: 'progress', component: ProgressWidget, spaces: ['chat', 'workspace'], fallback: 'status_card' },
  { name: 'code_block', component: CodeBlockWidget, spaces: ['chat', 'workspace'] },
  { name: 'status_card', component: StatusCardWidget, spaces: ['chat', 'workspace', 'floating'] },
  { name: 'decision', component: DecisionWidget, spaces: ['chat'], fallback: 'form' },
  { name: 'file_tree', component: FileTreeWidget, spaces: ['chat', 'workspace'], fallback: 'table' },
  { name: 'tree', component: FileTreeWidget, spaces: ['chat', 'workspace'], fallback: 'table' },
  { name: 'html_preview', component: HtmlPreviewWidget, spaces: ['workspace', 'floating', 'fullscreen'], fallback: 'code_block' },
  { name: 'review_document', component: ReviewDocumentWidget, spaces: ['workspace', 'fullscreen'], fallback: 'table' },
  { name: 'task_card', component: TaskCardWidget, spaces: ['chat', 'workspace', 'floating'], fallback: 'status_card' },
  { name: 'artifact_preview', component: ArtifactPreviewWidget, spaces: ['chat', 'workspace', 'floating'], fallback: 'code_block' },
  { name: 'editor', component: EditorWidget, spaces: ['chat', 'workspace', 'floating'], fallback: 'code_block' },
  { name: 'terminal', component: TerminalWidget, spaces: ['workspace', 'fullscreen'], fallback: 'code_block' },
  { name: 'kanban', component: KanbanWidget, spaces: ['workspace'], fallback: 'table' },
  {
    name: 'cost_dashboard',
    component: CostDashboardWidget,
    spaces: ['workspace', 'floating'],
    fallback: 'status_card',
  },
  // 顶栏打开的工作区面板（可关闭页签，非常驻）
  { name: 'settings_hub', component: SettingsHubPanel, spaces: ['workspace', 'floating'] },
  { name: 'plugins_panel', component: PluginsPanel, spaces: ['workspace'] },
  { name: 'monitoring_panel', component: MonitoringPanel, spaces: ['workspace'] },
  { name: 'tools_panel', component: ToolsPanel, spaces: ['workspace'] },
  { name: 'agents_panel', component: AgentsPanel, spaces: ['workspace'] },
  { name: 'memory_panel', component: MemoryPanel, spaces: ['workspace'] },
  { name: 'workspace_explorer', component: WorkspaceExplorerPanel, spaces: ['workspace'] },
  // 兼容 SettingsHubWidget 直注册
  { name: 'settings_hub_widget', component: SettingsHubWidget, spaces: ['workspace'] },
  // Webview：VS Code 风格插件自由 UI 沙箱（ADR §3.4'），fallback 到 html_preview
  { name: 'webview', component: WebviewWidget, spaces: ['workspace', 'floating', 'fullscreen'], fallback: 'html_preview' },
]

/**
 * 初始化所有预置组件注册
 *
 * 同时注册到：
 * 1. composerRegistry — 消息渲染管道（聊天消息中的 Widget）
 * 2. widgetRegistry — RenderingEngine 独立渲染（工作区/浮层中的 Widget）
 */
export function initializeWidgets(): void {
  for (const { name, component, spaces, fallback } of WIDGETS) {
    // 注册到 Composer 的 registry（消息渲染管道）
    composerRegistry.register(name, {
      component: component as React.ComponentType<Record<string, unknown>>,
      supportedSpaces: spaces,
      fallbackWidget: fallback,
    })
    // 注册到 WidgetRegistry（RenderingEngine 独立渲染）
    widgetRegistry.register(name, component as WidgetComponent, {
      name,
      supportedSpaces: spaces as Array<'chat' | 'workspace' | 'floating' | 'dock' | 'fullscreen'>,
      fallbackWidget: fallback,
    })
  }
}
