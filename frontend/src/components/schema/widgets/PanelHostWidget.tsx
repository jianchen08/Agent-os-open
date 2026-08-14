/**
 * 页面嵌入宿主 · 把现有全页组件嵌进工作区 tab
 *
 * 去掉原页 h-screen，改为 h-full，便于作为 WorkspacePanel 内容。
 */

import { cn } from '@/lib/utils'
import { PluginsSettingsPage } from '@/pages/settings/PluginsSettingsPage'
import { MonitoringPage } from '@/pages/monitoring/MonitoringPage'
import { ToolsPage } from '@/pages/tools/ToolsPage'
import { AgentsPage } from '@/pages/agents/AgentsPage'
import { MemoryPage } from '@/pages/memory/MemoryPage'
import { SettingsHubWidget } from './SettingsHubWidget'
import { CostDashboardWidget } from './CostDashboardWidget'
import { FileTreeWidget } from './FileTreeWidget'
import { FolderOpen } from '@/assets/icons'

type PanelKind =
  | 'settings_hub'
  | 'plugins_panel'
  | 'monitoring_panel'
  | 'tools_panel'
  | 'agents_panel'
  | 'memory_panel'
  | 'workspace_explorer'
  | 'cost_dashboard'

/**
 * 统一工作区面板宿主
 * props.panel 或 props 自身作为 kind（register 时用不同 name 复用同一组件时看 name）
 */
export function PanelHostWidget(props: Record<string, unknown>) {
  const kind = String(props.panel || props.kind || props.widget || '') as PanelKind
  return (
    <div className="h-full min-h-0 overflow-auto [&_[class*='h-screen']]:h-full">
      {renderPanel(kind, props)}
    </div>
  )
}

function renderPanel(kind: PanelKind | string, props: Record<string, unknown>) {
  switch (kind) {
    case 'settings_hub':
      return <SettingsHubWidget {...props} />
    case 'plugins_panel':
      return <PluginsSettingsPage />
    case 'monitoring_panel':
      return <MonitoringPage />
    case 'tools_panel':
      return <ToolsPage />
    case 'agents_panel':
      return <AgentsPage />
    case 'memory_panel':
      return <MemoryPage />
    case 'cost_dashboard':
      return <CostDashboardWidget {...props} />
    case 'workspace_explorer':
      return <WorkspaceExplorerPanel />
    default:
      // 按 component 名直达
      if (kind === 'settings_hub' || !kind) {
        return <SettingsHubWidget {...props} />
      }
      return (
        <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
          未知面板：{kind || '(empty)'}
        </div>
      )
  }
}

/** 各 panel 专用薄包装，便于 widgetRegistry 按名注册 */
export function SettingsHubPanel(props: Record<string, unknown>) {
  return <PanelHostWidget {...props} panel="settings_hub" />
}
export function PluginsPanel(props: Record<string, unknown>) {
  return <PanelHostWidget {...props} panel="plugins_panel" />
}
export function MonitoringPanel(props: Record<string, unknown>) {
  return <PanelHostWidget {...props} panel="monitoring_panel" />
}
export function ToolsPanel(props: Record<string, unknown>) {
  return <PanelHostWidget {...props} panel="tools_panel" />
}
export function AgentsPanel(props: Record<string, unknown>) {
  return <PanelHostWidget {...props} panel="agents_panel" />
}
export function MemoryPanel(props: Record<string, unknown>) {
  return <PanelHostWidget {...props} panel="memory_panel" />
}
export function WorkspaceExplorerPanel() {
  return (
    <div
      className={cn('flex h-full flex-col')}
      style={{ background: 'var(--ds-bg-panel, hsl(var(--card)))' }}
      data-testid="workspace-explorer"
    >
      <div
        className="flex items-center gap-2 border-b px-4 py-2"
        style={{ borderColor: 'var(--ds-border-subtle, rgba(148,163,184,0.12))' }}
      >
        <FolderOpen className="text-[var(--ds-accent-primary,#22D3EE)] h-4 w-4" />
        <div>
          <div className="text-foreground text-[13px] font-semibold">工作区</div>
          <div className="text-muted-foreground font-mono text-[10px]">
            文件管理 · 文件查看
          </div>
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-auto p-3">
        {/* pipelineView：统一管道管理（任务树超集）——任务树 + 所有执行中的管道（会话/任务）
            实时状态/耗时/token，点击管道行打开对应标签。dataSource 由任务/容器注入时
            用真实 workspace:// 显示文件树；此处无数据源时仅显示管道管理视图。 */}
        <FileTreeWidget
          showSearch
          expandLevel={1}
          nodeTitleField="name"
          nodeChildrenField="children"
          dataSource={undefined}
          pipelineView
        />
        <p className="text-muted-foreground mt-4 text-center text-xs">
          打开会话任务工作空间后，文件树将显示在此处。也可通过聊天中的文件卡片打开文件查看。
        </p>
      </div>
    </div>
  )
}
