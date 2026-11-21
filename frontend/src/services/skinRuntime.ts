/**
 * 平台皮肤运行时（皮肤能力是平台能力，非 DSH 专属）
 *
 * 任何插件的主题声明 `skin: "<皮肤资产id>"` 即获得全部皮肤能力：
 * 1. 平台 scope 打标：<html data-skin="{pluginId}:{skin}">（皮肤 CSS/JS 的
 *    作用域锚点）+ 暗色变体开关 body[data-skin-dark]；
 * 2. 皮肤 CSS 按择注入：经 /ext/{pluginId}/styles/skin/{skin}/merged.css 拉取
 *    （插件递送，可含翻译层），消毒 + nonce 后注入 <style>（data-theme-style
 *    命名空间，生命周期归主题切换）；
 * 3. 皮肤 hooks 运行时：拉取同路径 hooks.mjs（默认导出工厂 defineSkinHooks()
 *    → { apply(ctx) }），blob 导入运行（动态 import 无法带 Authorization 头）；
 *    ctx 提供六层装饰层/assetBase/theme/onCleanup（DSH skin-center 契约形态
 *    v1alpha1，作为平台皮肤脚本契约采用）；加载不得有顶层副作用，任何失败
 *    只影响动态层（静态 CSS 照常），apply 半途失败按已注册清理回滚；
 * 4. 装饰槽位预留：hooks 建的 fixed 全宽条（标题栏/状态栏类）量高后写入
 *    --skin-chrome-top/bottom 让位，切换/摘除收回。
 *
 * DSH 皮肤（dsh_adapter 的 16 款）= 本能力的第一个消费者：适配器在递送层把
 * DSH 词汇（data-dsh-skin/data-pane/data-slot 等）翻译成平台与我方锚点，
 * 本运行对 DSH 零特判。
 */
import { apiClient } from '@/services/api/client'
import { EXT_ROUTE } from '@/services/api/extRoute'
import { sanitizeCss, getStyleNonce } from '@/services/pluginStyles'
import { useSkinConsentStore } from '@/stores/skinConsentStore'
import { loggers } from '@/utils/logger'
import type { PluginTheme } from '@/types/theme'

// 独立属性命名空间：主题按择注入的 <style> 不带 data-plugin-style 属性，
// 不参与 syncPluginStyles 的声明清单清理（该清理按 data-plugin-style 比对）；
// 生命周期归主题切换（apply/clear 同一模块）
const STYLE_ID_ATTR = 'data-theme-style'
const PLUGIN_ATTR = 'data-plugin'

/** 是否为带皮肤资产的主题（声明驱动：任何插件声明 skin 字段即生效） */
export function isSkinTheme(
  theme: { skin?: string; pluginId?: string } | null | undefined,
): theme is PluginTheme & { skin: string } {
  return typeof theme?.skin === 'string' && theme.skin.length > 0 && !!theme.pluginId
}

// ── hooks 启用确认 + 消费点指纹 pin（2026-09-25 准入分级波2）────────────────
// hooks.mjs 在宿主同源上下文执行，是插件→前端最危险通道：首次启用与指纹漂移
// 须用户确认（确认卡挂 skinConsentStore），pin 打在**消费点**——dsh 递送层对
// hooks.mjs 做运行时选择器转译（ADR-0822），源文件哈希≠实际执行文本，只有对
// fetch 到的文本算哈希才锚住真实载荷。

/** 皮肤 merged.css 体积上限（静态样式层） */
export const MAX_SKIN_CSS_BYTES = 256 * 1024

const PIN_STORAGE_KEY = 'agentos_skin_hook_pins'

/** 读全部 hooks 指纹 pin（scope → sha256 hex；localStorage 损坏按空处理） */
export function loadSkinHookPins(): Record<string, string> {
  try {
    const raw = localStorage.getItem(PIN_STORAGE_KEY)
    return raw ? (JSON.parse(raw) as Record<string, string>) : {}
  } catch {
    return {}
  }
}

/** 写单条 pin（用户确认后调用） */
export function saveSkinHookPin(scope: string, hash: string): void {
  const pins = loadSkinHookPins()
  pins[scope] = hash
  try {
    localStorage.setItem(PIN_STORAGE_KEY, JSON.stringify(pins))
  } catch (e) {
    loggers.websocket.warn(`[skinRuntime] pin 持久化失败: ${(e as Error)?.message ?? e}`)
  }
}

export type SkinHookDecision =
  | { action: 'run' }
  | { action: 'confirm'; hash: string; reason: 'first' | 'drift'; previous?: string }

/** 纯判定：hooks 文本指纹 vs pin。首启用/漂移 → 需确认；一致 → 直接运行。 */
export function decideSkinHook(
  scope: string,
  hash: string,
  pins: Record<string, string>,
): SkinHookDecision {
  const pinned = pins[scope]
  if (!pinned) return { action: 'confirm', hash, reason: 'first' }
  if (pinned !== hash) return { action: 'confirm', hash, reason: 'drift', previous: pinned }
  return { action: 'run' }
}

/** hooks 文本 sha256（hex；crypto.subtle 在 jsdom/旧环境缺席时抛错由调用方兜底） */
async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
}

/** scope 值（html[data-skin] 的属性值；全局唯一 = 插件:皮肤） */
function scopeValueOf(theme: PluginTheme & { skin: string }): string {
  return `${theme.pluginId}:${theme.skin}`
}

/** 摘除当前皮肤注入（属性 + <style> + hooks 清理 + 装饰层清空 + 槽位收回） */
function disposeCurrentSkin(): void {
  // 无在挂皮肤即无事可清——关键守卫：themeStore.applyTheme 对非皮肤主题
  // 每次都会走 clearPluginSkin，无条件摘除会把同轮 image 分支刚挂上的
  // body.has-bg-image 拆掉（背景图主题的图片层从此不渲染）。
  if (!document.documentElement.hasAttribute('data-skin')) return
  document.documentElement.removeAttribute('data-skin')
  document.body.removeAttribute('data-skin-dark')
  // 皮肤激活期挂的背景图信号摘除（与主题管线互斥 owner，见 applyPluginSkin）
  document.body.classList.remove('has-bg-image')
  document
    .querySelectorAll(`style[${STYLE_ID_ATTR}^="skin-"]`)
    .forEach((el) => el.remove())
  disposeSkinHooks()
  stopChromeInsetSync()
  document.documentElement.style.removeProperty('--skin-chrome-top')
  document.documentElement.style.removeProperty('--skin-chrome-bottom')
}

/** 摘除（公开出口：切到非皮肤主题时 themeStore 调用） */
export function clearPluginSkin(): void {
  applyGeneration += 1
  disposeCurrentSkin()
}

/** 当前请求的皮肤与代际（切换竞态守卫：async 链路上被更新的切换/
    同皮肤二次应用取代的旧请求必须中止——同皮肤重复 apply 只存最新一代） */
let applyGeneration = 0

/**
 * 按择注入皮肤全量 CSS + 运行 hooks。切换语义：先摘旧皮肤（hooks 清理 +
 * 样式 + 槽位）再装新——皮肤间直切不再堆叠；async 续尾经代际校验。
 */
export async function applyPluginSkin(theme: PluginTheme & { skin: string }): Promise<void> {
  const scope = scopeValueOf(theme)
  const key = `skin-${scope}`
  const gen = ++applyGeneration
  disposeCurrentSkin()
  document.documentElement.setAttribute('data-skin', scope)
  if (theme.base === 'dark') document.body.setAttribute('data-skin-dark', '')
  else document.body.removeAttribute('data-skin-dark')
  // 背景图信号：皮肤激活期统一挂 body.has-bg-image（hooks 画背景的皮肤与
  // 内置背景主题走同一"背景图上需卡面"规则）；themeStore 的 image 分支
  // 在皮肤激活时不覆盖（互斥 owner，见 themeStore.applyTheme）
  document.body.classList.add('has-bg-image')

  try {
    const url = `${EXT_ROUTE}/${theme.pluginId}/styles/skin/${theme.skin}/merged.css`
    const res = await apiClient.get<string>(url, {
      responseType: 'text',
      transformResponse: [(d) => d],
    })
    if (applyGeneration !== gen) return // 已被更新的切换取代
    const css = typeof res.data === 'string' ? res.data : String(res.data)
    // 体积上限（2026-09-25）：静态样式层超限整段拒绝
    if (css.length > MAX_SKIN_CSS_BYTES) {
      loggers.websocket.warn(
        `[skinRuntime] ${scope} merged.css 超限（${css.length} > ${MAX_SKIN_CSS_BYTES} 字节），拒绝注入`,
      )
      return
    }
    const clean = sanitizeCss(css)
    if (clean === null) {
      loggers.websocket.warn(`[skinRuntime] ${scope} 命中危险 CSS 构造，拒绝注入`)
      return
    }
    const el = document.createElement('style')
    el.setAttribute(STYLE_ID_ATTR, key)
    el.setAttribute(PLUGIN_ATTR, theme.pluginId)
    const nonce = getStyleNonce()
    if (nonce) el.setAttribute('nonce', nonce)
    el.textContent = clean
    document.head.appendChild(el)
    loggers.websocket.debug(`[skinRuntime] 注入 ${scope} (${css.length} bytes)`)

    // === 皮肤 hooks（动态层：装饰 DOM/背景/标题栏/光标等由脚本建造）===
    // 加载不得有顶层副作用——fetch 文本 → blob 导入（同源带 Bearer 无法通过
    // import() 头传递）。任何失败只影响动态层，静态样式照常。
    if (applyGeneration === gen) await runSkinHooks(theme, scope, gen)
    if (applyGeneration === gen) {
      reserveChromeStrips()
      startChromeInsetSync()
    }
  } catch (e) {
    loggers.websocket.warn(`[skinRuntime] ${scope} 加载失败: ${(e as Error)?.message ?? e}`)
  }
}

/** 六层装饰层（皮肤脚本的注入位：background/ambient/top/bottom/sidebar/foreground，
    全部 pointer-events none，层序固定——采用 skin-center 契约形态） */
const LAYER_NAMES = ['background', 'ambient', 'top', 'bottom', 'sidebar', 'foreground'] as const
type SkinLayerName = (typeof LAYER_NAMES)[number]
const LAYER_Z: Record<SkinLayerName, number> = {
  background: 0,
  ambient: 1,
  sidebar: 40,
  top: 900,
  bottom: 901,
  foreground: 1000,
}

let layersHost: HTMLDivElement | null = null

function ensureSkinLayers(): Record<SkinLayerName, HTMLElement> {
  if (!layersHost || !document.body.contains(layersHost)) {
    layersHost = document.createElement('div')
    layersHost.id = 'skin-layers'
    layersHost.style.cssText = 'position: fixed; inset: 0; pointer-events: none; z-index: 0;'
    for (const name of LAYER_NAMES) {
      const layer = document.createElement('div')
      layer.setAttribute('data-skin-layer', name)
      layer.style.cssText = `position: absolute; inset: 0; pointer-events: none; z-index: ${LAYER_Z[name]}; overflow: hidden;`
      layersHost.appendChild(layer)
    }
    document.body.appendChild(layersHost)
  }
  const map = {} as Record<SkinLayerName, HTMLElement>
  for (const name of LAYER_NAMES) {
    map[name] = layersHost.querySelector(`[data-skin-layer="${name}"]`) as HTMLElement
  }
  return map
}

function clearSkinLayers(): void {
  if (layersHost) {
    for (const layer of layersHost.children) {
      layer.replaceChildren()
    }
  }
}

/** chrome 条带判定高度上限（标题栏/状态栏/垂坠带；超过即非条带，如全屏立绘层） */
const CHROME_STRIP_MAX_HEIGHT = 80

/** 图形垂坠纠位标记（量测落视口顶被我们钉回顶带下沿的条带；清理时按标记撤） */
const GRAPHIC_CLAMP_ATTR = 'data-skin-clamped-top'

/** 列让位目标标记（布局声明落点：聊天列/工作区列；皮肤运行时不认识具体组件） */
const INSET_TARGET_ATTR = 'data-skin-inset-target'
const INSET_TOP_VAR = '--skin-chat-inset-top'
const INSET_BOTTOM_VAR = '--skin-chat-inset-bottom'

/**
 * 槽位让位（注入不能遮住现有 UI）——两类 fixed 条带分开处理：
 * 1. **带文字内容的替代性条栏**（miku 标题栏/状态栏：顶替被删的顶栏/状态栏）
 *    → 整根让位 `--skin-chrome-top/bottom`（app 根下移/扣高）。
 * 2. **纯图形垂坠**（maid 顶/底花边：无文字、pointer-events none）→ 不整根让位
 *    （会把侧栏/工作区顶出空白带、页面被挤到中间），改为把与
 *    `[data-skin-inset-target]` 列的重叠高度写进 `--skin-chat-inset-top/bottom`，
 *    聊天/工作区内容避开垂坠带——花边悬在让位留白上，不压顶带标签行/输入卡底。
 *    垂坠条带若落在视口顶且跨过顶带下沿（皮肤按"标题栏行高"挂顶饰，布局未
 *    就绪时量到 0），钉回顶带下沿还原皮肤本意。
 * 条带允许横向偏移（maid 顶花边 translate 避让侧栏 left≠0）。
 */
function reserveChromeStrips(): void {
  const rootStyle = document.documentElement.style
  const vh = window.innerHeight
  const halfWidth = window.innerWidth / 2
  const bandEl = document.querySelector('[data-testid="chat-top-band"]')
  const bandBottom =
    bandEl instanceof HTMLElement ? Math.round(bandEl.getBoundingClientRect().bottom) : 0
  let top = 0
  let bottom = 0
  const graphics: Array<{ top: number; bottom: number }> = []
  for (const el of document.body.children) {
    if (!(el instanceof HTMLElement)) continue
    const cs = getComputedStyle(el)
    if (cs.position !== 'fixed') continue
    let rect = el.getBoundingClientRect()
    if (rect.height <= 0 || rect.height > CHROME_STRIP_MAX_HEIGHT) continue
    if (rect.width < halfWidth) continue
    if ((el.innerText ?? '').trim()) {
      // 替代性条栏：整根让位（按 top/bottom 锚定边判向）
      if (cs.top === '0px') top = Math.max(top, Math.round(rect.height))
      else if (cs.bottom === '0px') bottom = Math.max(bottom, Math.round(rect.height))
      continue
    }
    // 图形垂坠：量测落视口顶且跨过顶带下沿 → 钉回顶带下沿（每次同步先撤旧
    // 纠位再重量——皮肤自身量测恢复正确后交还其控制，幂等）
    if (el.hasAttribute(GRAPHIC_CLAMP_ATTR)) {
      el.style.removeProperty('top')
      el.removeAttribute(GRAPHIC_CLAMP_ATTR)
      rect = el.getBoundingClientRect()
    }
    if (bandBottom > 0 && rect.top < bandBottom && rect.bottom > bandBottom) {
      el.style.top = `${bandBottom}px`
      el.setAttribute(GRAPHIC_CLAMP_ATTR, '')
      rect = el.getBoundingClientRect()
    }
    const visibleTop = Math.max(rect.top, 0)
    const visibleBottom = Math.min(rect.bottom, vh)
    if (visibleBottom - visibleTop <= 0) continue
    graphics.push({ top: visibleTop, bottom: visibleBottom })
  }
  if (top > 0) rootStyle.setProperty('--skin-chrome-top', `${top}px`)
  else rootStyle.removeProperty('--skin-chrome-top')
  if (bottom > 0) rootStyle.setProperty('--skin-chrome-bottom', `${bottom}px`)
  else rootStyle.removeProperty('--skin-chrome-bottom')
  // 列让位：只算垂坠带与目标列相交的那段高度（列顶=顶带下沿，退场条带不占位）
  for (const target of document.querySelectorAll<HTMLElement>(`[${INSET_TARGET_ATTR}]`)) {
    const tr = target.getBoundingClientRect()
    if (tr.height <= 0) continue
    const mid = tr.top + tr.height / 2
    let insetTop = 0
    let insetBottom = 0
    for (const g of graphics) {
      const overlap = Math.min(g.bottom, tr.bottom) - Math.max(g.top, tr.top)
      if (overlap <= 0) continue
      if (g.top < mid) insetTop = Math.max(insetTop, Math.round(overlap))
      else insetBottom = Math.max(insetBottom, Math.round(overlap))
    }
    if (insetTop > 0) target.style.setProperty(INSET_TOP_VAR, `${insetTop}px`)
    else target.style.removeProperty(INSET_TOP_VAR)
    if (insetBottom > 0) target.style.setProperty(INSET_BOTTOM_VAR, `${insetBottom}px`)
    else target.style.removeProperty(INSET_BOTTOM_VAR)
  }
}

/** 让位重同步的延迟（皮肤状态机的 transform 过渡 .52s，落定后再量） */
const CHROME_INSET_SETTLE_MS = 600

let chromeInsetObserver: MutationObserver | null = null
let chromeInsetTimer: ReturnType<typeof setTimeout> | null = null

/** 状态变化重同步：body 属性=皮肤状态机（对话态退场等）、childList=hooks 建条带、
    顶带挂载=布局就绪（顶带在 #root 内，body childList 观察不到）。 */
function scheduleChromeInsetSync(): void {
  if (chromeInsetTimer !== null) clearTimeout(chromeInsetTimer)
  chromeInsetTimer = setTimeout(() => {
    chromeInsetTimer = null
    reserveChromeStrips()
  }, CHROME_INSET_SETTLE_MS)
}

function startChromeInsetSync(): void {
  if (chromeInsetObserver === null && typeof MutationObserver !== 'undefined') {
    chromeInsetObserver = new MutationObserver(scheduleChromeInsetSync)
    chromeInsetObserver.observe(document.body, { attributes: true, childList: true })
  }
  window.addEventListener('resize', scheduleChromeInsetSync)
  window.addEventListener('chat-top-band-mounted', scheduleChromeInsetSync)
}

/** 摘除重同步与全部让位/纠位写入（幂等） */
function stopChromeInsetSync(): void {
  chromeInsetObserver?.disconnect()
  chromeInsetObserver = null
  if (chromeInsetTimer !== null) {
    clearTimeout(chromeInsetTimer)
    chromeInsetTimer = null
  }
  window.removeEventListener('resize', scheduleChromeInsetSync)
  window.removeEventListener('chat-top-band-mounted', scheduleChromeInsetSync)
  for (const target of document.querySelectorAll<HTMLElement>(`[${INSET_TARGET_ATTR}]`)) {
    target.style.removeProperty(INSET_TOP_VAR)
    target.style.removeProperty(INSET_BOTTOM_VAR)
  }
  for (const el of document.querySelectorAll<HTMLElement>(`[${GRAPHIC_CLAMP_ATTR}]`)) {
    el.style.removeProperty('top')
    el.removeAttribute(GRAPHIC_CLAMP_ATTR)
  }
}

/** 当前激活的 hooks 清理注册表（切换/摘除时按序执行，幂等） */
let activeHookCleanups: Array<() => void> = []
let activeHookBlobUrl: string | null = null

/**
 * 运行皮肤 hooks：fetch hooks.mjs（/ext 通道带 Bearer）→ blob 导入 →
 * apply(ctx)。ctx 契约（skin-center v1alpha1 形态）：skinId/scopeAttr（=
 * html[data-skin="<scopeAttr>"] 的属性值，皮肤自行拼接，传裸值非选择器）
 * /assetBase（同源皮肤资产 URL 前缀）/layers 六层/theme/onCleanup。
 */
async function runSkinHooks(theme: PluginTheme & { skin: string }, scope: string, gen: number): Promise<void> {
  try {
    const res = await apiClient.get<string>(
      `${EXT_ROUTE}/${theme.pluginId}/styles/skin/${theme.skin}/hooks.mjs`,
      {
        responseType: 'text',
        transformResponse: [(d) => d],
      },
    )
    const text = typeof res.data === 'string' ? res.data : String(res.data)
    if (!text.trim()) {
      loggers.websocket.debug(`[skinRuntime] ${scope} 无 hooks.mjs，跳过动态层`)
      return
    }
    // 启用确认 + 消费点指纹 pin（2026-09-25）：首启用/漂移不执行，挂确认卡；
    // 静态 CSS 层不受影响（装饰主面已在，动态层等用户点头）。
    try {
      const hash = await sha256Hex(text)
      const decision = decideSkinHook(scope, hash, loadSkinHookPins())
      if (decision.action === 'confirm') {
        useSkinConsentStore.getState().setPending({
          theme,
          scope,
          hash: decision.hash,
          reason: decision.reason,
          previous: decision.previous,
        })
        loggers.websocket.info(
          `[skinRuntime] ${scope} hooks 待用户确认（${decision.reason}），跳过动态层`,
        )
        return
      }
    } catch (e) {
      // 指纹计算环境不可用（无 crypto.subtle）→ 保守拒绝动态层（fail-closed）
      loggers.websocket.warn(
        `[skinRuntime] ${scope} hooks 指纹计算失败，跳过动态层: ${(e as Error)?.message ?? e}`,
      )
      return
    }
    activeHookBlobUrl = URL.createObjectURL(new Blob([text], { type: 'text/javascript' }))
    const mod = await import(/* @vite-ignore */ activeHookBlobUrl)
    // 代际复查：import 期间的内部 await 可能已被更新的切换取代
    if (applyGeneration !== gen) {
      URL.revokeObjectURL(activeHookBlobUrl)
      activeHookBlobUrl = null
      return
    }
    // 契约形态：default 导出工厂（defineSkinHooks() → { apply(ctx) }）；
    // 兼容直接 default 出 apply 对象/函数
    let api: unknown = (mod as { default?: unknown })?.default ?? mod
    if (typeof api === 'function') api = (api as () => unknown)()
    const applyFn = (api as { apply?: (ctx: unknown) => void })?.apply
    if (typeof applyFn !== 'function') {
      loggers.websocket.debug(`[skinRuntime] ${scope} hooks 无 apply（default 工厂未产出），跳过`)
      return
    }
    const layers = ensureSkinLayers()
    const cleanups: Array<() => void> = []
    activeHookCleanups = cleanups
    const ctx = {
      skinId: theme.skin,
      // 契约：scopeAttr = 选择器 html[data-skin="<scopeAttr>"] 的属性值
      // （皮肤自行包裹：`html[data-skin="${ctx.scopeAttr}"]`——传完整选择器
      // 会套娃成非法 CSS，皮肤脚本 insertRule 当场抛、装饰全灭）
      scopeAttr: scope,
      // 契约：同源资产前缀，皮肤自拼 `${assetBase}/assets/<file>`——不带尾斜杠
      // （带尾斜杠拼出 // 路径，内核路由 404=背景图/立绘静默全灭）
      assetBase: `${EXT_ROUTE}/${theme.pluginId}/styles/skin-assets/${theme.skin}`,
      layers,
      theme: {
        get: () => (theme.base === 'light' ? 'light' : 'dark'),
        subscribe: () => () => {},
      },
      onCleanup: (fn: () => void) => cleanups.push(fn),
    }
    try {
      applyFn.call(api, ctx)
    } catch (e) {
      // apply 半途失败：皮肤在开头就注册了 onCleanup，不回滚会留下
      // 半应用状态（body 背景/变量已写、装饰没挂）——按已注册清理逆序回滚后
      // 重抛，由外层记日志（静态 CSS 层不受影响）
      for (const fn of [...cleanups].reverse()) {
        try {
          fn()
        } catch {
          /* 清理自身失败不再传播 */
        }
      }
      activeHookCleanups = []
      throw e
    }
    loggers.websocket.debug(`[skinRuntime] ${scope} hooks applied (${cleanups.length} cleanups)`)
  } catch (e) {
    loggers.websocket.warn(`[skinRuntime] ${scope} hooks 运行失败（静态层不受影响）: ${(e as Error)?.message ?? e}`)
  }
}

function disposeSkinHooks(): void {
  for (const fn of [...activeHookCleanups].reverse()) {
    try {
      fn()
    } catch (e) {
      loggers.websocket.warn(`[skinRuntime] hook cleanup 异常: ${(e as Error)?.message ?? e}`)
    }
  }
  activeHookCleanups = []
  if (activeHookBlobUrl) {
    URL.revokeObjectURL(activeHookBlobUrl)
    activeHookBlobUrl = null
  }
  clearSkinLayers()
}
