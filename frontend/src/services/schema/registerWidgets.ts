/** 组件注册初始化 将所有已实现的组件注册到两套 Widget Registry */

import { ArtifactPreviewWidget } from '@/components/schema/widgets/ArtifactPreviewWidget'
import { ChartWidget } from '@/components/schema/widgets/ChartWidget'
import { CodeBlockWidget } from '@/components/schema/widgets/CodeBlockWidget'
import { CostDashboardWidget } from '@/components/schema/widgets/CostDashboardWidget'
import { DigitalHumanWidget } from '@/components/schema/widgets/DigitalHumanWidget'
import {
  AgentsPanel,
  MemoryPanel,
  MonitoringPanel,
  PipelineManagerPanel,
  PluginsPanel,
  SettingsHubPanel,
  ToolsPanel,
} from '@/components/schema/widgets/PanelHostWidget'
import { PipelineManagerWidget } from '@/components/schema/widgets/PipelineManagerWidget'
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
import { widgetRegistry } from './WidgetRegistry'
import type { WidgetComponent } from './WidgetRegistry'
import type { RenderingSpaceType } from '@/types/schema'

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
  { name: 'workspace_explorer', component: PipelineManagerPanel, spaces: ['workspace'] },
  // 任务/管道管理（独立工作区标签，按需打开；旧 workspace_explorer 注册保留兼容）
  { name: 'pipeline_manager', component: PipelineManagerPanel, spaces: ['workspace'] },
  { name: 'pipeline_manager_widget', component: PipelineManagerWidget, spaces: ['workspace'] },
  // 兼容 SettingsHubWidget 直注册
  { name: 'settings_hub_widget', component: SettingsHubWidget, spaces: ['workspace'] },
  // Webview：VS Code 风格插件自由 UI 沙箱（ADR §3.4'），fallback 到 html_preview。
  // 注：webcomponent（WebComponentCardHost，eval 注入）已于 0.2 废弃并删除代码，
  // 插件自定义完整 UI 一律走 webview / 主题插件 / CSS 注入（task_plugin_frontend_customization）。
  { name: 'webview', component: WebviewWidget, spaces: ['workspace', 'floating', 'fullscreen'], fallback: 'html_preview' },
  // 数字人/形象占位 widget（ADR §2.1 / §7.6）：形象是 workspace 的 widget，不占独立空间。
  // 现阶段只占位（不引入渲染库），0.7.0 由插件接入 Live2D/VRM/TTS；支持 detachable 三态（浮窗/桌面组件/全屏）
  {
    name: 'digital_human',
    component: DigitalHumanWidget,
    spaces: ['workspace', 'floating', 'fullscreen'],
    fallback: 'status_card',
  },
]

/**
 * 初始化所有预置组件注册
 *
 * composer 已收敛至 WidgetRegistry（composer.tsx re-export 同一单例），
 * 消息渲染管道与 RenderingEngine 共用此唯一注册表，注册一次即可。
 */
export function initializeWidgets(): void {
  for (const { name, component, spaces, fallback } of WIDGETS) {
    const supportedSpaces = spaces as RenderingSpaceType[]
    widgetRegistry.register(name, component as WidgetComponent, {
      name,
      supportedSpaces,
      fallbackWidget: fallback,
    })
  }
}
