/** 组件注册初始化 将所有已实现的组件注册到两套 Widget Registry */

import { ImageAnnotationView } from '@/components/approval/ImageAnnotationView'
import { MediaTimelineView } from '@/components/approval/MediaTimelineView'
import { ArtifactPreviewWidget } from '@/components/schema/widgets/ArtifactPreviewWidget'
import { ChartWidget } from '@/components/schema/widgets/ChartWidget'
import { CodeBlockWidget } from '@/components/schema/widgets/CodeBlockWidget'
import { ContextUsageWidget } from '@/components/schema/widgets/ContextUsageWidget'
import { DebugCenterHubWidget } from '@/components/schema/widgets/DebugCenterHubWidget'
import { DigitalHumanWidget } from '@/components/schema/widgets/DigitalHumanWidget'
import { EditorWidget } from '@/components/schema/widgets/EditorWidget'
import { FileTreeWidget } from '@/components/schema/widgets/FileTreeWidget'
import { DecisionFormAdapter, FormWidget } from '@/components/schema/widgets/FormWidget'
import { GalleryWidget } from '@/components/schema/widgets/GalleryWidget'
import { HtmlPreviewWidget } from '@/components/schema/widgets/HtmlPreviewWidget'
import { InlineEditWidget } from '@/components/schema/widgets/InlineEditWidget'
import { KanbanWidget } from '@/components/schema/widgets/KanbanWidget'
import {
  AgentsPanel,
  PipelineManagerPanel,
  SettingsHubPanel,
} from '@/components/schema/widgets/PanelHostWidget'
import { ReviewDocumentWidget } from '@/components/schema/widgets/ReviewDocumentWidget'
import { SortableListWidget } from '@/components/schema/widgets/SortableListWidget'
import { StatusCardWidget } from '@/components/schema/widgets/StatusCardWidget'
import { TableWidget } from '@/components/schema/widgets/TableWidget'
import { TerminalWidget } from '@/components/schema/widgets/TerminalWidget'
import { WebviewWidget } from '@/components/schema/widgets/WebviewWidget'
import { WidgetStage } from '@/components/schema/widgets/WidgetStage'
import { WizardWidget } from '@/components/schema/widgets/WizardWidget'
import { TextDiffView } from '@/components/shared/TextDiffView'
import { ContractStatusPanel } from '@/components/debug/ContractStatusPanel'
import { DbAdminPage } from '@/pages/debug/DbAdminPage'
import { DebugEvaluationMetricsPage } from '@/pages/debug/DebugEvaluationMetricsPage'
import { DebugExecutionRecordsPage } from '@/pages/debug/DebugExecutionRecordsPage'
import { DebugLlmPayloadPage } from '@/pages/debug/DebugLlmPayloadPage'
import { DebugPipelineStatePage } from '@/pages/debug/DebugPipelineStatePage'
import { DebugSessionsPage } from '@/pages/debug/DebugSessionsPage'
import { DebugTasksPage } from '@/pages/debug/DebugTasksPage'
import { DebugUsersPage } from '@/pages/debug/DebugUsersPage'
import { KnowledgeBasePage } from '@/pages/knowledge-base/KnowledgeBasePage'
import { LlmSettingsPage } from '@/pages/settings/LlmSettingsPage'
import { MemoryPage } from '@/pages/memory/MemoryPage'
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

/** 模型设置面板（llm_service 插件 contributes.pages space=settings 声明承载；
 *  原内核导航「模型」页迁移，设置中枢唯一模型配置入口） */
const LlmSettingsWidget = () => <LlmSettingsPage embedded />

/** 调试中心子页（debug_center 插件 space=debug_center 页声明经 hub 组台引用；
 *  embedded 模态适配工作区面板，子页本体随 /debug 路由族退役仅存此通道） */
const DebugDbAdminWidget = () => <DbAdminPage embedded />
const DebugExecutionRecordsWidget = () => <DebugExecutionRecordsPage embedded />
const DebugPipelineStateWidget = () => <DebugPipelineStatePage embedded />
const DebugSessionsWidget = () => <DebugSessionsPage embedded />
const DebugTasksWidget = () => <DebugTasksPage embedded />
const DebugUsersWidget = () => <DebugUsersPage embedded />
const DebugEvaluationMetricsWidget = () => <DebugEvaluationMetricsPage embedded />
const DebugLlmPayloadWidget = () => <DebugLlmPayloadPage embedded />

/** 记忆/知识库域页面（hindsight_memory 插件 space=workspace slot=tab 声明，
 *  /p/memory、/p/knowledge_base 全页渲染；数据面 = hindsight http_endpoints） */
const MemoryPanelWidget = () => <MemoryPage />
const KnowledgeBasePanelWidget = () => <KnowledgeBasePage />

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
  // 顶栏打开的工作区面板（可关闭页签，非常驻）
  { name: 'settings_hub', component: SettingsHubPanel, spaces: ['workspace', 'floating'] },
  // agents_panel = agent_manager 插件页面承载（原 AgentsPage 内容迁移；
  // tools_panel 已随 ToolsPage 退役，plugins_panel 独立面板随双入口收敛撤除——
  // 能力浏览并入设置中枢「插件注册表」kernel-plugins）
  { name: 'agents_panel', component: AgentsPanel, spaces: ['workspace'] },
  // memory_panel 死注册已摘除（P0-3）：hindsight_memory 不贡献页面声明
  // （侧边栏记忆页先前拍板移除），/memory 路由直挂 MemoryPage 不经本注册；
  // 记忆页声明化归 P3-4（hindsight 域插件声明页）。
  // 调试中心面板（debug_center 插件 contributes.pages 声明，单入口：仅管理员可见；
  // 面板内部切换 6 个调试页面——数据库管理/执行记录/会话/任务/用户/评估指标，
  // 页面数据经各数据源插件 HTTP 面获取（db_admin|monitoring|evaluation_service|task_service 等）
  { name: 'debug_center_hub', component: DebugCenterHubWidget, spaces: ['workspace'] },
  // 任务/管道管理（独立工作区标签，按需打开；pipeline_manager 是唯一注册名）
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
  // 声明 widget 组台（contributes.pages 按 widget: 'widget_stage' 路由）：
  // 渲染指定 space 的全部声明 widget + G4 受控桥
  { name: 'widget_stage', component: WidgetStage, spaces: ['workspace', 'floating', 'fullscreen'] },
  // 富交互形态（widget 化 G5）：多步向导 / 拖拽排序 / 内联编辑
  { name: 'wizard', component: WizardWidget, spaces: ['workspace', 'floating', 'fullscreen'], fallback: 'form' },
  { name: 'sortable_list', component: SortableListWidget, spaces: ['workspace', 'floating'], fallback: 'table' },
  { name: 'inline_edit', component: InlineEditWidget, spaces: ['chat', 'workspace'], fallback: 'form' },
  // 审批三视图（widget 化 T10：review_service 的 ui.view_modes 声明路由复用件；
  // 生产渲染唯一通道 = 本注册表按名解析）
  { name: 'text_diff', component: TextDiffWidget, spaces: ['workspace', 'fullscreen'], fallback: 'code_block' },
  { name: 'image_annotation', component: ImageAnnotationWidget, spaces: ['workspace', 'fullscreen'], fallback: 'text_diff' },
  { name: 'media_timeline', component: MediaTimelineWidget, spaces: ['workspace', 'fullscreen'], fallback: 'text_diff' },
  // 输入框上下文用量指示器（chat-input 空间 context_usage 槽位）：数据从管道
  // state 读（共享 pipelineStates query，事件失效化实时刷新），组件按当前管道
  // 提取 track.llm_usage / llm_model。spaces 用 'chat'——chat-input 不是
  // RenderingSpaceType 枚举值（槽位语义靠声明 space 过滤，解析只按 type 查表）
  { name: 'context_usage', component: ContextUsageWidget, spaces: ['chat'] },
  // 模型设置（llm_service 插件声明 settings 页，原内核导航「模型」迁移而来）
  { name: 'llm_settings', component: LlmSettingsWidget, spaces: ['workspace'] },
  // 调试中心子页（debug_center 插件 space=debug_center 声明 → debug_center_hub 组台；
  // 注册名是子页唯一前端锚点，清单/顺序/图标归插件声明）
  { name: 'debug_db_admin', component: DebugDbAdminWidget, spaces: ['workspace'] },
  { name: 'debug_execution_records', component: DebugExecutionRecordsWidget, spaces: ['workspace'] },
  { name: 'debug_pipeline_state', component: DebugPipelineStateWidget, spaces: ['workspace'] },
  { name: 'debug_sessions', component: DebugSessionsWidget, spaces: ['workspace'] },
  { name: 'debug_tasks', component: DebugTasksWidget, spaces: ['workspace'] },
  { name: 'debug_users', component: DebugUsersWidget, spaces: ['workspace'] },
  { name: 'debug_evaluation', component: DebugEvaluationMetricsWidget, spaces: ['workspace'] },
  { name: 'debug_llm_payload', component: DebugLlmPayloadWidget, spaces: ['workspace'] },
  { name: 'debug_contract_status', component: ContractStatusPanel, spaces: ['workspace'] },
  // 记忆/知识库域（hindsight_memory 声明页承载，预置域 widget——交互复杂度
  // 超出声明组台能力，验收标准=入口声明+数据面插件化）
  { name: 'memory_panel', component: MemoryPanelWidget, spaces: ['workspace'] },
  { name: 'knowledge_base_panel', component: KnowledgeBasePanelWidget, spaces: ['workspace'] },
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
