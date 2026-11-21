/**
 * DSH 客户端组件宿主（组件级挂载，不是页面翻译）。
 *
 * DSH 插件的前端是 client bundle：`window.__ModuleLoader__.load({id, factory})`
 * 包装的模块，`apply(ctx)` 里用 `ctx.slots.register({name, ...}, Component)`
 * 往客户端槽位注册 React 组件（bundle 自己 `require('react')`，不自带 React）。
 *
 * 本服务提供挂载所需的三个面，组件由插件原样提供、原样渲染：
 * 1. 模块加载器：全局 `__ModuleLoader__` + `require` 映射（React/ReactDOM/
 *    jsx-runtime 用宿主实例，`styles` 为空实现）；
 * 2. slots 服务：`inject(name, cb)` / `register(meta, component)` 的最小语义
 *    （官方 `SlotCore` 的接口子集；全量槽位契约待接官方 shell 时替换）；
 * 3. ctx：`inject/get/provide/effect/locale` 的最小实现——缺服务时 `inject`
 *    不执行回调（插件按 DSH 语义自行降级），不伪造服务。
 *
 * 边界：只服务「已映射槽位」的渲染；未映射槽位由后端清单如实报告，不生成
 * 占位页。bundle 源码经内核 ext 路由带 Bearer 拉取（不裸开静态目录）。
 */

import * as React from 'react'
import * as JSXRuntime from 'react/jsx-runtime'
import * as ReactDOM from 'react-dom'
import { createRoot } from 'react-dom/client'
import { apiClient } from '@/services/api/client'
import { extUrl } from '@/services/api/extRoute'
import { contributionRegistry, type ContributionEntry } from '@/services/schema/ContributionRegistry'
import { loggers } from '@/utils/logger'

/** 后端 client-ui 清单里的单个槽位注册（静态扫描结果）。 */
export interface DshClientSlot {
  name: string
  id: string | null
  label: string | null
  order: number | null
}

/** 后端生成的页面声明（槽位 → 灵汐页面位）。 */
export interface DshClientPage {
  id: string
  title: string
  space: string
  slot: string
  widget: string
  props: Record<string, unknown>
}

/** 单个已装包的客户端 UI 贡献。 */
export interface DshClientBundle {
  bundle: string
  version: string
  entry: string
  entry_url: string
  scanned: boolean
  slots: DshClientSlot[]
  injects: string[]
  /** bundle 的宿主依赖名（require 扫描结果） */
  requires?: string[]
  /** 宿主未提供的依赖（非空 = 当前渲染面接不住，不生成页面） */
  blocked?: string[]
  pages: DshClientPage[]
  unmapped: string[]
}

/** 槽位注册项（渲染侧消费）。 */
export interface DshSlotEntry {
  name: string
  id: string | null
  order: number | null
  component: unknown
  /** 注册声明的 locale 命名空间（DSH 标准 seat：组件 props.t 按此绑定） */
  localeNs: string | null
}

/** bundle 应用结果（诊断用）。 */
export interface DshBundleRuntime {
  bundle: string
  applied: boolean
  missingServices: string[]
  entries: DshSlotEntry[]
  /** 该 bundle apply 时的 locale 服务（组件 props.t 按注册的命名空间取词） */
  locale: DshLocaleService | null
}

/** 模块工厂签名（DSH client bundle 契约）。 */
type ModuleFactory = (require: (name: string) => unknown) => Record<string, unknown>
/** 已装载模块表：包名 → module.exports。 */
const loadedModules = new Map<string, Record<string, unknown>>()

/** `require` 映射：插件 bundle 的宿主依赖。 */
const REQUIRE_MAP: Record<string, unknown> = {
  react: React,
  'react-dom': { ...ReactDOM, createRoot },
  'react-dom/client': { createRoot },
  'react/jsx-runtime': JSXRuntime,
  'react/jsx-dev-runtime': JSXRuntime,
  styles: {},
}

/** 全局模块加载器（幂等；多 bundle 共用一张模块表）。 */
function ensureModuleLoader(): void {
  const holder = window as unknown as { __ModuleLoader__?: { load: (spec: { id: string; factory: ModuleFactory }) => void } }
  if (holder.__ModuleLoader__) return
  holder.__ModuleLoader__ = {
    load: ({ id, factory }) => {
      loadedModules.set(id, factory((name) => {
        if (!(name in REQUIRE_MAP)) throw new Error(`dsh client bundle 依赖未提供: ${name}`)
        return REQUIRE_MAP[name]
      }))
    },
  }
}

/** 清单缓存（一次会话一次拉取；失败不缓存，便于重试）。 */
let manifestPromise: Promise<DshClientBundle[]> | null = null

/** 拉取已装包的客户端 UI 贡献清单。 */
export function loadClientUiManifest(force = false): Promise<DshClientBundle[]> {
  if (force) manifestPromise = null
  if (!manifestPromise) {
    manifestPromise = apiClient
      .get<{ bundles?: DshClientBundle[] }>(extUrl('dsh_adapter', '/api/dsh/client-ui'))
      .then((res) => res.data?.bundles ?? [])
      .catch((error: unknown) => {
        manifestPromise = null
        loggers.websocket.warn('[dshClientHost] client-ui 清单拉取失败', error)
        return []
      })
  }
  return manifestPromise
}

/** 取单个包的清单条目。 */
export async function findClientBundle(bundle: string): Promise<DshClientBundle | null> {
  const bundles = await loadClientUiManifest()
  return bundles.find((item) => item.bundle === bundle) ?? null
}

/** 用 blob 脚本执行 bundle 源码（避免 eval；bundle 自带 `__ModuleLoader__` 调用）。 */
async function runBundleScript(source: string, bundle: string): Promise<void> {
  const url = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }))
  try {
    await new Promise<void>((resolve, reject) => {
      const el = document.createElement('script')
      el.src = url
      el.dataset.dshBundle = bundle
      el.onload = () => resolve()
      el.onerror = () => reject(new Error(`client bundle 脚本执行失败: ${bundle}`))
      document.head.appendChild(el)
    })
  } finally {
    URL.revokeObjectURL(url)
  }
}

/**
 * bundle 脚本执行器（模块级可替换）：jsdom 不加载 blob script 资源（onload
 * 永不触发），测试注入桩执行器；生产恒用真身。
 */
let bundleRunner: (source: string, bundle: string) => Promise<void> = runBundleScript

/** 测试用：替换/还原 bundle 脚本执行器。 */
export function __setBundleRunnerForTests(runner: ((source: string, bundle: string) => Promise<void>) | null): void {
  bundleRunner = runner ?? runBundleScript
}

/** slots 服务：DSH 槽位注册/依赖语义的最小实现。 */
export class DshSlotService {
  private readonly entries: DshSlotEntry[] = []

  /** 槽位就绪回调（DSH 语义：槽位由宿主声明后回调；本宿主全部视为就绪）。 */
  inject(name: string, callback: () => void): void {
    loggers.websocket.debug?.('[dshClientHost] slots.inject', name)
    callback()
  }

  /** 注册一个槽位组件（id/key 任一作为贡献标识）。 */
  register(meta: Record<string, unknown>, component: unknown): void {
    const name = typeof meta.name === 'string' ? meta.name : ''
    if (!name || component == null) return
    const id = typeof meta.id === 'string' ? meta.id : typeof meta.key === 'string' ? meta.key : null
    const order = typeof meta.order === 'number' ? meta.order : null
    const localeNs = typeof meta.locale === 'string' ? meta.locale : null
    this.entries.push({ name, id, order, component, localeNs })
  }

  /** 取某槽位的注册项（按 order 升序，order 缺省排后）。 */
  entriesOf(name: string): DshSlotEntry[] {
    return this.entries
      .filter((entry) => entry.name === name)
      .sort((a, b) => (a.order ?? Number.MAX_SAFE_INTEGER) - (b.order ?? Number.MAX_SAFE_INTEGER))
  }

  /** 全部注册项（诊断用）。 */
  all(): DshSlotEntry[] {
    return [...this.entries]
  }
}

/**
 * locale 服务：DSH 客户端 locale 的最小忠实实现。
 *
 * DSH 词表契约：`locale.register(ns, { en: {...}, zh: {...} })`——顶层键是
 * 语言标签，内层才是「键 → 文案」。`locale.bind(ns)` 取绑定翻译函数，按
 * 首选语言解析（缺语言回退 en，再回退键名）。宿主不编造文案。
 */
const PREFERRED_LOCALE_KEY = '__dshPreferredLocale'

/** 首选语言（默认 zh；宿主接 i18n 后经 setPreferredLocale 跟随）。 */
function preferredLocale(): string {
  try {
    return (globalThis as Record<string, unknown>)[PREFERRED_LOCALE_KEY] as string ?? 'zh'
  } catch {
    return 'zh'
  }
}

/** 设置首选语言（供宿主语言切换时同步）。 */
export function setPreferredLocale(locale: string): void {
  try {
    ;(globalThis as Record<string, unknown>)[PREFERRED_LOCALE_KEY] = locale
  } catch {
    // 无法写全局时保持默认语言（只影响取词顺序，不阻断挂载）
  }
}

export class DshLocaleService {
  private readonly dictionaries = new Map<string, Record<string, Record<string, string>>>()

  /** 注册某命名空间的词表（顶层语言标签 → 键表；非语言表整体记入 `*` 档）。 */
  register(namespace: string, dictionary: unknown): void {
    const perLang: Record<string, Record<string, string>> = { ...(this.dictionaries.get(namespace) ?? {}) }
    if (isLanguageTable(dictionary)) {
      for (const [lang, table] of Object.entries(dictionary as Record<string, unknown>)) {
        const flat: Record<string, string> = {}
        flattenDictionary(table, '', flat)
        perLang[lang] = { ...(perLang[lang] ?? {}), ...flat }
      }
    } else {
      const flat: Record<string, string> = {}
      flattenDictionary(dictionary, '', flat)
      perLang['*'] = { ...(perLang['*'] ?? {}), ...flat }
    }
    this.dictionaries.set(namespace, perLang)
  }

  /** 绑定命名空间的翻译函数（`{name}` 模板参数替换；缺词回退键名）。 */
  bind(namespace: string): (key: string, params?: Record<string, unknown>) => string {
    return (key, params) => this.t(namespace, key, params)
  }

  /** 翻译：首选语言 → en → 其余语言 → 键名。 */
  t(namespace: string, key: string, params?: Record<string, unknown>): string {
    const perLang = this.dictionaries.get(namespace)
    if (!perLang) return key
    const order = [preferredLocale(), 'en', '*']
    for (const lang of [...order, ...Object.keys(perLang)]) {
      const raw = perLang[lang]?.[key]
      if (typeof raw === 'string') return interpolate(raw, params)
    }
    return key
  }
}

/** 语言表判定：非空对象且每个值都是普通对象（DSH 契约形态）。 */
function isLanguageTable(value: unknown): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false
  const entries = Object.entries(value as Record<string, unknown>)
  return entries.length > 0 && entries.every(([, v]) => typeof v === 'object' && v !== null && !Array.isArray(v))
}

/** 词表展平：`{a:{b:'x'}}` → `{'a.b':'x'}`（数组/标量原样字符串化）。 */
function flattenDictionary(value: unknown, prefix: string, out: Record<string, string>): void {
  if (typeof value === 'string') {
    out[prefix] = value
    return
  }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    for (const [key, inner] of Object.entries(value as Record<string, unknown>)) {
      flattenDictionary(inner, prefix ? `${prefix}.${key}` : key, out)
    }
    return
  }
  if (prefix) out[prefix] = String(value)
}

/** `{name}` 模板参数替换（缺参数保留占位原样）。 */
function interpolate(template: string, params: Record<string, unknown> | undefined): string {
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (_match, name: string) => String(params[name] ?? `{${name}}`))
}

/** bundle 应用缓存：同包只 apply 一次（DSH 客户端语义：apply 幂等由宿主保证）。 */
const runtimePromises = new Map<string, Promise<DshBundleRuntime>>()

/** 装载并 apply 一个插件 client bundle，返回槽位注册结果。 */
export function applyClientBundle(bundle: string): Promise<DshBundleRuntime> {
  const cached = runtimePromises.get(bundle)
  if (cached) return cached
  const task = (async (): Promise<DshBundleRuntime> => {
    const info = await findClientBundle(bundle)
    if (!info) return { bundle, applied: false, missingServices: ['(未安装或未启用)'], entries: [], locale: null }
    ensureModuleLoader()
    try {
      const res = await apiClient.get<string>(info.entry_url, {
        responseType: 'text',
        transformResponse: [(data) => data],
      })
      const source = typeof res.data === 'string' ? res.data : String(res.data)
      await bundleRunner(source, bundle)
    } catch (error) {
      loggers.websocket.warn('[dshClientHost] bundle 装载失败', bundle, error)
      runtimePromises.delete(bundle)
      return { bundle, applied: false, missingServices: [`(装载失败) ${String(error)}`], entries: [], locale: null }
    }
    const mod = loadedModules.get(bundle)
    if (!mod || typeof mod.apply !== 'function') {
      runtimePromises.delete(bundle)
      return { bundle, applied: false, missingServices: ['(模块未注册 apply)'], entries: [], locale: null }
    }
    const localeService = new DshLocaleService()
    const slots = new DshSlotService()
    const services: Record<string, unknown> = { slots, locale: localeService }
    const declared = Array.isArray(mod.inject) ? mod.inject.filter((n): n is string => typeof n === 'string') : []
    const missing = declared.filter((name) => !(name in services))
    const ctx = createContext(slots, services, missing, localeService)
    try {
      ;(mod.apply as (context: unknown) => void)(ctx)
    } catch (error) {
      loggers.websocket.warn('[dshClientHost] bundle apply 失败', bundle, error)
      runtimePromises.delete(bundle)
      return { bundle, applied: false, missingServices: [`(apply 失败) ${String(error)}`], entries: [], locale: null }
    }
    return { bundle, applied: true, missingServices: missing, entries: slots.all(), locale: localeService }
  })()
  runtimePromises.set(bundle, task)
  return task
}

/** 最小 ctx：`inject` 只在依赖齐备时回调（缺服务不伪造）。 */
function createContext(
  slots: DshSlotService,
  services: Record<string, unknown>,
  missing: string[],
  localeService: DshLocaleService,
): Record<string, unknown> {
  const disposers: Array<() => void> = []
  const ctx: Record<string, unknown> = {
    slots,
    get: (name: string) => services[name],
    provide: (name: string, value: unknown) => {
      services[name] = value
    },
    effect: (fn: () => unknown) => {
      const dispose = fn()
      if (typeof dispose === 'function') disposers.push(dispose as () => void)
      return () => {
        const index = disposers.indexOf(dispose as () => void)
        if (index >= 0) disposers.splice(index, 1)
      }
    },
    inject: (deps: string[] | string, callback: (scoped: Record<string, unknown>) => void) => {
      const list = Array.isArray(deps) ? deps : [deps]
      if (list.every((name) => name in services)) callback(ctx)
      else missing.push(...list.filter((name) => !(name in services)))
    },
    locale: localeService,
    on: () => () => {},
    emit: () => {},
  }
  return ctx
}

/** 已动态注册的槽位页：页面 id → 声明签名（安装状态变化时按签名重注册）。 */
const registeredPages = new Map<string, string>()

/**
 * 按 client-ui 清单动态注册/注销槽位页。
 *
 * 页面随「已装/已启用」运行态变化，所以不写进插件清单（plugin.json 只留
 * 插件自声明页）：清单里有 → 注册（声明变化则重注册），清单里没有 → 注销。
 * 只在有映射槽位时产生页面；未映射槽位不生成占位页。
 */
export async function syncClientUiPages(): Promise<{ registered: number; removed: number }> {
  const bundles = await loadClientUiManifest(true)
  // DSH 客户端语义：全部 client bundle 在 shell 启动时 apply——纯 CSS
  // （always-status-bar）、locale 注册、未映射槽位注册（prompt-snippets 的
  // conversation.input.right）等不生成页面的包也要生效。同包幂等
  // （runtimePromises 缓存）；apply 失败记入该包 runtime（fail-visible），
  // 不阻断页面注册。
  await Promise.allSettled(bundles.map((b) => applyClientBundle(b.bundle)))
  const next = new Map<string, DshClientPage>()
  for (const bundle of bundles) {
    for (const page of bundle.pages) next.set(page.id, page)
  }
  let removed = 0
  let registered = 0
  for (const [id, signature] of [...registeredPages]) {
    const page = next.get(id)
    if (page && JSON.stringify(page) === signature) continue
    contributionRegistry.unregister('pages', id)
    registeredPages.delete(id)
    if (!page) removed += 1
  }
  // 防御性清理：注册表里带本前缀但清单已无的页（跨会话残留/后端变更后的旧注册）
  for (const page of contributionRegistry.getPages()) {
    if (page.id.startsWith('dsh_ui_') && !next.has(page.id)) {
      contributionRegistry.unregister('pages', page.id)
      registeredPages.delete(page.id)
    }
  }
  for (const [id, page] of next) {
    if (registeredPages.has(id)) continue
    contributionRegistry.register({ ...page, type: 'pages', pluginId: 'dsh_adapter' } as unknown as ContributionEntry)
    registeredPages.set(id, JSON.stringify(page))
    registered += 1
  }
  return { registered, removed }
}

/** 测试用：清空模块/应用缓存。 */
export function __resetClientHostForTests(): void {
  loadedModules.clear()
  runtimePromises.clear()
  manifestPromise = null
  registeredPages.clear()
}
