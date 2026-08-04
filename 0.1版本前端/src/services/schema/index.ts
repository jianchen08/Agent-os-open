/**
 * Schema 服务模块
 *
 * 统一导出 Schema 解析、渲染引擎、Widget 注册表等核心服务。
 * 提供 React Hook 封装，简化组件层使用。
 *
 * @module schema
 */

// ===== Schema 注册表（已有）=====
export { schemaRegistry } from './registry'
export { parseSchema, parseDataSourceRef, resolveDataSource, validateSchema } from './parser'
export type { ParsedSchema } from './parser'
export { renderLayoutNode, widgetRegistry as composerWidgetRegistry } from './composer'
export type { LayoutNode, ComponentRenderer } from './composer'

// ===== Schema 解析器（新增）=====
export { SchemaParser, schemaParser } from './SchemaParser'
export type { SchemaParseError, ValidationResult, SchemaParserOptions } from './SchemaParser'

// ===== 渲染引擎（新增）=====
export { RenderingEngine, renderingEngine } from './RenderingEngine'
export type { RenderInstruction, RenderInstructionSet, RenderingEngineConfig } from './RenderingEngine'

// ===== Widget 注册表（新增）=====
export { widgetRegistry } from './WidgetRegistry'
export type {
  WidgetProps,
  WidgetComponent,
  WidgetMetadata,
  WidgetEntry,
} from './WidgetRegistry'


