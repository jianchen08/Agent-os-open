/**
 * WorkspaceHost —— 工作区空间宿主（架构 §5.4 workspace SpaceHost）
 *
 * 组合两部分，使工作区内容真正「声明驱动」：
 * 1. WorkspacePanel：Tab 式页面内容（手开的页签 / 文件编辑器）
 * 2. DeclaredWidgetLayer：插件在 ui_schema 声明、space=workspace 的常驻 widget
 *    （架构 §5.3 链路的生产消费终点——之前只挂在从不渲染的 PageRenderer 里，
 *     现经 WorkspaceHost 接入在跑的工作区，链路通电）
 *
 * 无声明 widget 时 DeclaredWidgetLayer 返回 null，WorkspaceHost 与原 WorkspacePanel
 * 行为一致；有声明时在 Tab 区下方追加渲染。
 *
 * 关联：docs/working/重要设计/前端能力统一架构.md §5.3 / §5.4
 */

import { DeclaredWidgetLayer } from '@/components/schema/DeclaredWidgetLayer'
import { WorkspacePanel } from './WorkspacePanel'
import type { WorkspacePanelProps } from './WorkspacePanel'

export function WorkspaceHost(props: WorkspacePanelProps) {
  return (
    <>
      <WorkspacePanel {...props} />
      <DeclaredWidgetLayer space="workspace" />
    </>
  )
}
