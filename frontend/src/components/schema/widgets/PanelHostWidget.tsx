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
import { PipelineManagerWidget } from './PipelineManagerWidget'

type PanelKind =
  | 'settings_hub'
  | 'plugins_panel'
  | 'monitoring_panel'
  | 'tools_panel'
  | 'agents_panel'
  | 'memory_panel'
  | 'workspace_explorer'
  | 'pipeline_manager'

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
    case 'workspace_explorer':
    case 'pipeline_manager':
      return <PipelineManagerPanel />
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
/** 任务/管道管理面板：右侧面板直接展示（无标题栏包裹），
 *  统一管道管理视图：执行中的管道（任务/会话）实时状态 + 任务树组合。
 *  文件树通过任务节点"打开工作空间"按钮按需打开（0.1 语义）。 */
export function PipelineManagerPanel() {
  return (
    <div
      className={cn('flex h-full flex-col')}
      style={{ background: 'var(--ds-bg-panel, hsl(var(--card)))' }}
      data-testid="pipeline-manager"
    >
      <div className="min-h-0 flex-1">
        {/* PipelineManagerWidget：管道管理（内核快照 + 实时事件）+ 任务树组合 */}
        <PipelineManagerWidget />
      </div>
    </div>
  )
}
