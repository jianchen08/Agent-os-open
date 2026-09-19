/**
 * 通用引用 provider 注册缝
 *
 * 消息协议 `<reference source="..." k="v">- 名称 (类型) @ 路径</reference>`
 * 是源无关的：任何插件域（Godot 场景、文件、设计稿、外部对象…）都可产生
 * 引用。两个消费面均经本注册缝：注入侧（getSelection/consume，发送时拼接
 * 进消息正文）与展示侧（getRow/subscribeRow/activateRow，输入区实时镜像
 * 行）；provider 自持数据通道（轮询/WS/本地状态），ChatInput 壳与镜像行
 * 组件都不感知具体插件（ADR 2026-09-10-generic-reference-protocol）。
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

/** 输入区镜像行单个引用卡片（结构对齐 ReferenceChip 的数据契约） */
export interface ReferenceRowChip {
  /** 列表渲染 key（域内唯一） */
  key: string
  /** 引用类型徽章（如 'godot-node'，驱动 ReferenceChip 渲染器分发） */
  kind: string
  /** 主标题 */
  title: string
  /** 副标题（如 类型 @ 路径） */
  subtitle?: string
  /** 预览图 URL（可选） */
  previewUrl?: string
}

/** 输入区镜像行数据（provider 展示面；无待发引用时 getRow 返回 null 不渲染） */
export interface ReferenceRowState {
  /** 行标签（如 'Godot 引用'） */
  label: string
  /** 数据通道连接状态（行首圆点，缺省按未连接展示） */
  connected?: boolean
  chips: ReferenceRowChip[]
  /** 行尾 × 主动清除当前引用（失败由 provider 自持，不阻塞 UI） */
  clear(): void | Promise<void>
}

export interface ReferenceProvider {
  /** 引用来源标识（= 消息块 source 属性，如 'godot'） */
  source: string
  /** 当前待发引用（空时返回 null，不注入） */
  getSelection(): ReferenceSelection | null
  /** 发送后消耗（清理待发状态）；失败不阻塞发送 */
  consume?(): void | Promise<void>
  /** 输入区实时镜像行数据（threadId = 会话线程；无待发引用返回 null） */
  getRow?(threadId?: string): ReferenceRowState | null
  /** 镜像行变化通知（返回取消函数；提供 getRow 的 provider 应实现） */
  subscribeRow?(onChange: () => void): () => void
  /** 行宿主挂载/切线程时激活数据通道（订阅线程 + 拉快照） */
  activateRow?(threadId?: string): void | Promise<void>
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
