/**
 * SlotRegistry — 前端槽位扩展机制注册表（C4，设计真值 =
 * docs/working/DSH生态嫁接通用机制设计_20261104.md §4.4）
 *
 * 职责（三轴之「slot = 渲染组件怎么组织」的声明装载面）：
 * - 装载 plugin_contributes 的 `contributes.slots` 首类声明（slot/order/when/
 *   title/channel/widget|htmlPath/props/detachable）
 * - 存量三面收敛翻译（2026-11-04 评审裁定）：`contributes.pages`(space=workspace)
 *   → `workspace.tab` keyed 条目、`contributes.modal` → `shell.overlay` 条目
 *   （触发面 contributes.commands + commandDispatcher 不动，动作面不进槽）
 * - fail-closed（AC-F2/AC-F5）：表外/未开放槽 key、channel 缺字段、keyed 缺 key、
 *   component 通道引用未注册 widget（装载期声明↔注册一致性）= 报错跳过该条目，
 *   不静默空白；错误收集可查（getLoadErrors，管理可见原因的取数面）
 * - W 通用递送衔接：声明引用 `<pluginId>/<name>` 动态 widget 时先经通用加载器
 *   装载插件产物，再校验注册一致性
 *
 * 渲染组织消费方：SlotHost（list 槽锚点）/ workspacePanelOpener（workspace.tab
 * keyed）/ SlotOverlayHost（shell.overlay 触发呈现）/ ToolviewCardHost（chat.toolview）。
 */

import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { isSlotKeyOpen, slotKindOf, type SlotKind } from './slotVocabulary'
import { isNamespacedWidgetName, loadPluginWidgets } from './widgetLoader'
import type { WidgetBundleModule } from './widgetSdk'

/** 渲染通道（三轴之 channel = 渲染组件怎么来） */
export type SlotChannel = 'component' | 'webview'

/** 脱离宿主容器配置（声明面无几何字段，仅 detachable 自管 defaultSize——尺寸协商契约） */
export interface SlotDetachable {
  popout?: boolean
  defaultSize?: { w: number; h: number }
}

/** 槽条目（装载校验后的规范形态；渲染侧唯一消费形态） */
export interface SlotEntry {
  /** 全局唯一 id：`{pluginId}:{声明 id}` */
  id: string
  /** 声明级 id（插件清单内的裸 id；工作区页签 id 稳定性锚——开签格式
   *  `ws-plugin-<declId>` 与存量 pages 消费一致，持久化页签零抖动） */
  declId?: string
  /** 槽 key（词表内且开放） */
  slot: string
  /** kind（从词表继承） */
  kind: SlotKind
  /** keyed 槽的键（workspace.tab=path；chat.toolview=卡型名）；list 槽无 */
  key?: string
  /** 显示标题（零域词表：标题/标签随声明下发） */
  title?: string
  /** 图标（随声明下发） */
  icon?: string
  channel: SlotChannel
  /** component 通道：widget 名（内建白名单名或动态 `<pluginId>/<name>`） */
  widget?: string
  /** webview 通道：HTML 路径（相对插件根，伺服于 /ext/<pluginId> 下） */
  htmlPath?: string
  /** 静态 props（与 pages widget props 同构；跨域只传 JSON） */
  props?: Record<string, unknown>
  /** 排序权重（小者前；缺省 100） */
  order?: number
  /** 可见条件（when 表达式，渲染侧求值） */
  when?: string
  /** overlay 触发器（shell.overlay 域：`on_command:<commandId>`；触发面=commands） */
  openOn?: string
  /** 脱离宿主配置（popout/defaultSize） */
  detachable?: SlotDetachable
  /** 来源插件 ID */
  pluginId: string
}

/** 装载错误（条目粒度；fail-closed 跳过原因，管理可见性取数面） */
export interface SlotLoadError {
  pluginId: string
  /** 被跳过条目的合成标识（声明 id 或槽内序号） */
  entryId?: string
  reason: string
}

/** 装载依赖缝：W 加载器可注入（测试/宿主变体；importBundle 为产物网络边界替身） */
export interface SlotLoaderDeps {
  loadWidgets?: (pluginId: string) => Promise<SlotLoadError[]>
  importBundle?: (pluginId: string) => Promise<WidgetBundleModule>
}

/** 缺省排序权重 */
const DEFAULT_ORDER = 100

/** plugin.json 条目原始形态 */
type RawItem = Record<string, unknown>

/** 装载中间态（校验通过结构、待 widget 存在性终验的条目） */
interface ParsedEntry {
  entry: SlotEntry
}

class SlotRegistry {
  private entries: SlotEntry[] = []
  /** keyed 槽索引：slotKey → (key → entry) */
  private keyed = new Map<string, Map<string, SlotEntry>>()
  /** overlay 触发索引：trigger → entry */
  private overlayByTrigger = new Map<string, SlotEntry>()
  private loadErrors: SlotLoadError[] = []
  private version = 0
  private listeners = new Set<() => void>()

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  getVersion = (): number => {
    return this.version
  }

  private bumpVersion(): void {
    this.version += 1
    for (const listener of this.listeners) listener()
  }

  /**
   * 从 /api/v1/schema 装载槽声明（幂等重载：先清后装，防幽灵条目）。
   *
   * 流程：结构解析（逐条 fail-closed）→ W 动态装载（引用动态命名空间的插件）
   * → widget 存在性终验 → 建索引并通知订阅方。
   */
  async loadFromSchema(
    schema: {
      plugin_contributes?: Array<{
        plugin_id?: string
        contributes?: Record<string, unknown[]>
      }>
    },
    deps?: SlotLoaderDeps,
  ): Promise<void> {
    this.clear()
    const pluginList = schema.plugin_contributes ?? []
    const parsed: ParsedEntry[] = []

    for (const plugin of pluginList) {
      const pluginId = plugin.plugin_id
      if (!pluginId || !plugin.contributes) continue
      // 首类声明：contributes.slots
      const slots = plugin.contributes.slots
      if (Array.isArray(slots)) {
        slots.forEach((item, idx) => {
          this.parseSlotEntry(item as RawItem, pluginId, idx, parsed)
        })
      }
      // 存量收敛翻译：contributes.modal → shell.overlay
      const modals = plugin.contributes.modal
      if (Array.isArray(modals)) {
        modals.forEach((item, idx) => {
          this.translateModalEntry(item as RawItem, pluginId, idx, parsed)
        })
      }
      // 存量收敛翻译：contributes.pages(space=workspace) → workspace.tab
      const pages = plugin.contributes.pages
      if (Array.isArray(pages)) {
        pages.forEach((item) => {
          this.translatePageEntry(item as RawItem, pluginId, parsed)
        })
      }
    }

    // W 动态装载：声明引用 `<pluginId>/<name>` 的插件先装载产物（注册进
    // widgetRegistry 动态源），装载错误并入 loadErrors
    const dynamicPlugins = new Set<string>()
    for (const p of parsed) {
      if (p.entry.channel === 'component' && isNamespacedWidgetName(p.entry.widget ?? '')) {
        dynamicPlugins.add(p.entry.pluginId)
      }
    }
    if (dynamicPlugins.size > 0) {
      const load =
        deps?.loadWidgets ??
        ((pid: string) => loadPluginWidgets(pid, { importBundle: deps?.importBundle }))
      const results = await Promise.all([...dynamicPlugins].map((pid) => load(pid)))
      for (const errs of results) this.loadErrors.push(...errs)
    }

    // widget 存在性终验（装载期声明↔注册一致性，AC-F5 fail-closed）
    const valid: ParsedEntry[] = []
    for (const p of parsed) {
      if (p.entry.channel === 'component' && !widgetRegistry.has(p.entry.widget ?? '')) {
        this.loadErrors.push({
          pluginId: p.entry.pluginId,
          entryId: p.entry.id,
          reason: `槽条目引用未注册 widget "${p.entry.widget}"（装载期声明↔注册一致性校验失败，条目跳过）`,
        })
        continue
      }
      valid.push(p)
    }

    // 排序（order 缺省殿后，同 order 按 id 稳定序）+ 建索引
    valid.sort(
      (a, b) =>
        (a.entry.order ?? DEFAULT_ORDER) - (b.entry.order ?? DEFAULT_ORDER) ||
        a.entry.id.localeCompare(b.entry.id),
    )
    for (const p of valid) {
      const e = p.entry
      this.entries.push(e)
      if (e.kind === 'keyed' && e.key !== undefined) {
        let bucket = this.keyed.get(e.slot)
        if (!bucket) {
          bucket = new Map()
          this.keyed.set(e.slot, bucket)
        }
        if (!bucket.has(e.key)) bucket.set(e.key, e)
      }
      if (e.slot === 'shell.overlay' && e.openOn && !this.overlayByTrigger.has(e.openOn)) {
        this.overlayByTrigger.set(e.openOn, e)
      }
    }
    this.bumpVersion()
  }

  /** list 语义查询：某槽域全部条目（order 稳定序；keyed 槽亦可用=全量按键条目） */
  getSlotEntries(slotKey: string): SlotEntry[] {
    return this.entries.filter((e) => e.slot === slotKey)
  }

  /** keyed 语义查询：按键取条目（keyed 槽专属） */
  getKeyedEntry(slotKey: string, key: string): SlotEntry | undefined {
    return this.keyed.get(slotKey)?.get(key)
  }

  /** shell.overlay 触发查询（commandDispatcher 命令触发呈现） */
  findOverlayByTrigger(trigger: string): SlotEntry | undefined {
    return this.overlayByTrigger.get(trigger)
  }

  /** 全部条目（调试/管理面取数） */
  allEntries(): SlotEntry[] {
    return [...this.entries]
  }

  /** 装载错误（fail-closed 跳过原因） */
  getLoadErrors(): SlotLoadError[] {
    return [...this.loadErrors]
  }

  /** 完全清空（登出/销毁） */
  clear(): void {
    this.entries = []
    this.keyed.clear()
    this.overlayByTrigger.clear()
    this.loadErrors = []
    this.bumpVersion()
  }

  // ── 内部：首类 contributes.slots 解析（逐条 fail-closed）──

  private parseSlotEntry(item: RawItem, pluginId: string, idx: number, out: ParsedEntry[]): void {
    const entryId = typeof item.id === 'string' && item.id ? `${pluginId}:${item.id}` : `${pluginId}:slot#${idx}`
    const fail = (reason: string): void => {
      this.loadErrors.push({ pluginId, entryId, reason })
    }
    if (!item || typeof item !== 'object') {
      fail('槽条目必须是对象')
      return
    }
    const slotKey = typeof item.slot === 'string' ? item.slot : ''
    if (!isSlotKeyOpen(slotKey)) {
      fail(`表外/未开放槽 key "${slotKey || '(空)'}"（词表见 slotVocabulary.ts，fail-closed 跳过）`)
      return
    }
    const channel = item.channel
    if (channel !== 'component' && channel !== 'webview') {
      fail(`channel 必须为 "component"|"webview"，收到 ${JSON.stringify(channel ?? null)}`)
      return
    }
    const kind = slotKindOf(slotKey) ?? 'list'
    let key: string | undefined
    if (kind === 'keyed') {
      key = typeof item.key === 'string' && item.key.trim() ? item.key : undefined
      if (!key) {
        fail(`keyed 槽 "${slotKey}" 条目缺少 key（按键实例语义必需）`)
        return
      }
    }
    let widget: string | undefined
    let htmlPath: string | undefined
    if (channel === 'component') {
      widget = typeof item.widget === 'string' && item.widget.trim() ? item.widget : undefined
      if (!widget) {
        fail('component 通道条目缺少 widget 名')
        return
      }
    } else {
      htmlPath = typeof item.htmlPath === 'string' && item.htmlPath.trim() ? item.htmlPath : undefined
      if (!htmlPath) {
        fail('webview 通道条目缺少 htmlPath')
        return
      }
    }
    out.push({
      entry: {
        id: entryId,
        declId: typeof item.id === 'string' && item.id ? item.id : undefined,
        slot: slotKey,
        kind,
        key,
        title: typeof item.title === 'string' ? item.title : undefined,
        icon: typeof item.icon === 'string' ? item.icon : undefined,
        channel,
        widget,
        htmlPath,
        props: isPlainObject(item.props) ? item.props : undefined,
        order: typeof item.order === 'number' ? item.order : undefined,
        when: typeof item.when === 'string' ? item.when : undefined,
        openOn: typeof item.openOn === 'string' ? item.openOn : undefined,
        detachable: parseDetachable(item.detachable),
        pluginId,
      },
    })
  }

  // ── 内部：存量收敛翻译 ──

  /** contributes.modal → shell.overlay 条目（呈现=模态；触发面 commands 不动） */
  private translateModalEntry(item: RawItem, pluginId: string, idx: number, out: ParsedEntry[]): void {
    const entryId = `${pluginId}:modal#${idx}`
    const widget = typeof item.widget === 'string' && item.widget.trim() ? item.widget : undefined
    if (!widget) {
      this.loadErrors.push({ pluginId, entryId, reason: 'modal 条目缺少 widget（收敛翻译为 shell.overlay 后无法渲染，跳过）' })
      return
    }
    out.push({
      entry: {
        id: `${pluginId}:overlay:${typeof item.id === 'string' ? item.id : `#${idx}`}`,
        declId: typeof item.id === 'string' ? item.id : undefined,
        slot: 'shell.overlay',
        kind: 'list',
        title: typeof item.title === 'string' ? item.title : undefined,
        icon: typeof item.icon === 'string' ? item.icon : undefined,
        channel: 'component',
        widget,
        props: isPlainObject(item.props) ? item.props : undefined,
        order: typeof item.order === 'number' ? item.order : undefined,
        when: typeof item.when === 'string' ? item.when : undefined,
        openOn: typeof item.openOn === 'string' ? item.openOn : (typeof item.trigger === 'string' ? item.trigger : undefined),
        detachable: parseDetachable(item.detachable),
        pluginId,
      },
    })
  }

  /**
   * contributes.pages(space=workspace) → workspace.tab keyed 条目（key=path??id）
   *
   * 无 widget 的 pages 条目静默跳过：pages 还承载导航容器/设置页等非页签内容
   * 声明（导航/标签机制是收敛保留物），缺 widget = 非页签内容声明而非坏声明。
   */
  private translatePageEntry(item: RawItem, pluginId: string, out: ParsedEntry[]): void {
    if (!item || typeof item !== 'object') return
    if (item.space !== 'workspace') return
    const widget = typeof item.widget === 'string' && item.widget.trim() ? item.widget : undefined
    if (!widget) return
    const id = typeof item.id === 'string' && item.id ? item.id : `page#${out.length}`
    const key = typeof item.path === 'string' && item.path.trim() ? item.path : id
    out.push({
      entry: {
        id: `${pluginId}:tab:${id}`,
        declId: id,
        slot: 'workspace.tab',
        kind: 'keyed',
        key,
        title: typeof item.title === 'string' ? item.title : undefined,
        icon: typeof item.icon === 'string' ? item.icon : undefined,
        channel: 'component',
        widget,
        props: isPlainObject(item.props) ? item.props : undefined,
        order: typeof item.order === 'number' ? item.order : undefined,
        when: typeof item.when === 'string' ? item.when : undefined,
        detachable: parseDetachable(item.detachable),
        pluginId,
      },
    })
  }
}

/** detachable 解析：声明面无几何字段，仅收 popout/defaultSize（尺寸协商契约） */
function parseDetachable(raw: unknown): SlotDetachable | undefined {
  if (!isPlainObject(raw)) return undefined
  const out: SlotDetachable = {}
  if (raw.popout === false) out.popout = false
  if (isPlainObject(raw.defaultSize) && typeof raw.defaultSize.w === 'number' && typeof raw.defaultSize.h === 'number') {
    out.defaultSize = { w: raw.defaultSize.w, h: raw.defaultSize.h }
  }
  return Object.keys(out).length > 0 ? out : undefined
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** 全局单例 */
export const slotRegistry = new SlotRegistry()

export default slotRegistry
