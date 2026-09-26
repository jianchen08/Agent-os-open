/**
 * 通用引用服务入口
 *
 * 引用协议（消息块 `<reference source=...>`）源无关：注入侧 provider 注册
 * 缝在此聚合。host provider 是首个注册件（包装 pipeline_host_context 的
 * hostBridge 数据通道，连接者身份由插件配置指定）——新插件域只需
 * registerReferenceProvider 自带 provider 即可让选中引用随消息发送并被
 * 通用渲染（ADR 2026-09-10、2026-09-24-host-context-generic-connection）。
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
import './hostProvider'
