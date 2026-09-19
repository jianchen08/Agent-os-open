/**
 * 通用引用服务入口
 *
 * 引用协议（消息块 `<reference source=...>`）源无关：注入侧 provider 注册
 * 缝在此聚合。godot provider 是首个注册件（包装 pipeline_godot_context 的
 * selectionBridge 数据通道）——新插件域只需 registerReferenceProvider 自带
 * provider 即可让选中引用随消息发送并被通用渲染（ADR 2026-09-10）。
 */
export {
  buildReferenceBlock,
  getReferenceProviders,
  registerReferenceProvider,
  type ReferenceProvider,
  type ReferenceSelection,
  type ReferenceSelectionItem,
  type ReferenceRowState,
} from './referenceProviders'

// 首个 provider 注册（模块副作用：引用面就绪即注入可用）
import './godotProvider'
