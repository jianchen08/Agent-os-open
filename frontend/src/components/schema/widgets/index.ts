/**
 * 聊天交互组件 barrel export
 *
 * 统一导出所有 Schema 驱动的聊天交互组件。
 * 注：ProgressWidget/TaskCardWidget 已并入 StatusCardWidget（卡片三形态）；
 * DecisionWidget 已并入 FormWidget（DecisionFormAdapter）。
 */
export { FormWidget, DecisionFormAdapter } from './FormWidget'
export { ChartWidget } from './ChartWidget'
export { GalleryWidget } from './GalleryWidget'
export { TableWidget } from './TableWidget'
export { StatusCardWidget } from './StatusCardWidget'
export { CodeBlockWidget } from './CodeBlockWidget'
export { KanbanWidget } from './KanbanWidget'
export { EditorWidget } from './EditorWidget'
export { TerminalWidget } from './TerminalWidget'
export { FileTreeWidget } from './FileTreeWidget'
export { HtmlPreviewWidget } from './HtmlPreviewWidget'
export { ReviewDocumentWidget } from './ReviewDocumentWidget'
export { ArtifactPreviewWidget } from './ArtifactPreviewWidget'
