/**
 * 通用引用 provider 注册缝
 *
 * 消息协议 `<reference source="..." k="v">- 名称 (类型) @ 路径</reference>`
 * 是源无关的：任何插件域（Godot 场景、文件、设计稿、外部对象…）都可产生
 * 引用。注入侧（发送时拼接进消息正文）经本注册缝枚举各 source 的待发
 * 引用；provider 自持数据通道（轮询/WS/本地状态），ChatInput 壳不感知
 * 具体插件（ADR 2026-09-10-generic-reference-protocol）。
 */

/** 单个引用条目（消息块行 `- 名称 (类型) @ 路径 [附加]`） */
export interface ReferenceSelectionItem {
  name: string
  type: string
  path: string
  /** 行内附加段（原样写入 `[...]`，如 `position=(1,2,3)`） */
  extra?: string
}

/** 一个 source 的待发引用快照 */
export interface ReferenceSelection {
  source: string
  /** 引用块头部附加属性（序列化为 `k="v"`，如 Godot scene） */
  attrs?: Record<string, string>
  items: ReferenceSelectionItem[]
}

export interface ReferenceProvider {
  /** 引用来源标识（= 消息块 source 属性，如 'godot'） */
  source: string
  /** 当前待发引用（空时返回 null，不注入） */
  getSelection(): ReferenceSelection | null
  /** 发送后消耗（清理待发状态）；失败不阻塞发送 */
  consume?(): void | Promise<void>
}

const providers = new Map<string, ReferenceProvider>()

/** 注册引用 provider（同 source 后注册覆盖） */
export function registerReferenceProvider(provider: ReferenceProvider): void {
  providers.set(provider.source, provider)
}

/** 全部已注册 provider（按注册序） */
export function getReferenceProviders(): ReferenceProvider[] {
  return [...providers.values()]
}

/** 拼接引用消息块（与 parseReferenceMessage 解析互逆；空 items 不产出） */
export function buildReferenceBlock(sel: ReferenceSelection): string | null {
  if (sel.items.length === 0) return null
  const attrs = Object.entries(sel.attrs ?? {})
    .map(([k, v]) => ` ${k}="${v}"`)
    .join('')
  const lines = sel.items.map(
    (it) => `- ${it.name} (${it.type}) @ ${it.path}${it.extra ? ` [${it.extra}]` : ''}`,
  )
  return [`<reference source="${sel.source}"${attrs}>`, ...lines, '</reference>'].join('\n')
}
