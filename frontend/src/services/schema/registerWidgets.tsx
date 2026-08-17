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
import { DecisionFormAdapter, FormWidget } from '@/components/schema/widgets/FormWidget'
import { EditorWidget } from '@/components/schema/widgets/EditorWidget'
import { FileTreeWidget } from '@/components/schema/widgets/FileTreeWidget'
import { GalleryWidget } from '@/components/schema/widgets/GalleryWidget'
import { HtmlPreviewWidget } from '@/components/schema/widgets/HtmlPreviewWidget'
import { KanbanWidget } from '@/components/schema/widgets/KanbanWidget'
import { ReviewDocumentWidget } from '@/components/schema/widgets/ReviewDocumentWidget'
import { StatusCardWidget } from '@/components/schema/widgets/StatusCardWidget'
import { TableWidget } from '@/components/schema/widgets/TableWidget'
import { TerminalWidget } from '@/components/schema/widgets/TerminalWidget'
import { WebviewWidget } from '@/components/schema/widgets/WebviewWidget'
import { DebugCenterHubWidget } from '@/components/schema/widgets/DebugCenterHubWidget'
import { ImageAnnotationView } from '@/components/approval/ImageAnnotationView'
import { MediaTimelineView } from '@/components/approval/MediaTimelineView'
import { TextDiffView } from '@/components/approval/TextDiffView'
import { widgetRegistry } from './WidgetRegistry'
import type { WidgetComponent } from './WidgetRegistry'
import type { Annotation } from '@/types/review'
import type { RenderingSpaceType } from '@/types/schema'

/** 审批三视图 widget 适配（widget 化 T10：view_mode 声明路由的复用件） */
const TextDiffWidget = (props: Record<string, unknown>) => (
  <TextDiffView
    oldContent={typeof props.oldContent === 'string' ? props.oldContent : ''}
    newContent={typeof props.newContent === 'string' ? props.newContent : ''}
  />
)
const ImageAnnotationWidget = (props: Record<string, unknown>) => (
  <ImageAnnotationView
    imageUrl={typeof props.imageUrl === 'string' ? props.imageUrl : ''}
    annotations={(props.annotations as Annotation[]) ?? []}
    readOnly={props.readOnly === true}
  />
)
const MediaTimelineWidget = (props: Record<string, unknown>) => (
  <MediaTimelineView
    mediaUrl={typeof props.mediaUrl === 'string' ? props.mediaUrl : ''}
    mediaType={props.mediaType === 'audio' ? 'audio' : 'video'}
    duration={typeof props.duration === 'number' ? props.duration : undefined}
    annotations={(props.annotations as Annotation[]) ?? []}
    readOnly={props.readOnly === true}
  />
)

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
  // 卡片三形态统一组件（variant/props 推断：metric/progress/task；progress/task_card
  // 注册名别名已清理——零消费，声明确要旧名时用 status_card + variant 参数）
  { name: 'status_card', component: StatusCardWidget, spaces: ['chat', 'workspace', 'floating'] },
  { name: 'code_block', component: CodeBlockWidget, spaces: ['chat', 'workspace'] },
  // 决策选择 = 单字段表单（radio/checkbox，字段模式点选即回调）
  { name: 'decision', component: DecisionFormAdapter, spaces: ['chat'], fallback: 'form' },
  { name: 'file_tree', component: FileTreeWidget, spaces: ['chat', 'workspace'], fallback: 'table' },
  { name: 'html_preview', component: HtmlPreviewWidget, spaces: ['workspace', 'floating', 'fullscreen'], fallback: 'code_block' },
  { name: 'review_document', component: ReviewDocumentWidget, spaces: ['workspace', 'fullscreen'], fallback: 'table' },
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
  // 调试中心面板（debug_center 插件 contributes.pages 声明，单入口：仅管理员可见；
  // 面板内部切换 6 个调试页面——数据库管理/执行记录/会话/任务/用户/评估指标，
  // 页面数据经各数据源插件 HTTP 面获取：/ext/db_admin|channel_api|monitoring|evaluation_service/*）
  { name: 'debug_center_hub', component: DebugCenterHubWidget, spaces: ['workspace'] },
  // 任务/管道管理（独立工作区标签，按需打开；workspace_explorer 旧注册名已清理）
  { name: 'pipeline_manager', component: PipelineManagerPanel, spaces: ['workspace'] },
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
  // 审批三视图（widget 化 T10：review_service 的 ui.view_modes 声明路由复用件；
  // ApprovalRouter 未声明时直连内置组件，不依赖本注册）
  { name: 'text_diff', component: TextDiffWidget, spaces: ['workspace', 'fullscreen'], fallback: 'code_block' },
  { name: 'image_annotation', component: ImageAnnotationWidget, spaces: ['workspace', 'fullscreen'], fallback: 'text_diff' },
  { name: 'media_timeline', component: MediaTimelineWidget, spaces: ['workspace', 'fullscreen'], fallback: 'text_diff' },
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
