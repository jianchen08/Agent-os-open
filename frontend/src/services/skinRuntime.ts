/**
 * 平台皮肤运行时（2026-08-22 用户裁决：皮肤能力是平台能力，非 DSH 专属）
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
import { loggers } from '@/utils/logger'
import { sanitizeCss, getStyleNonce } from '@/services/pluginStyles'
import type { PluginTheme } from '@/types/theme'

// 独立属性命名空间：主题按择注入的 <style> 不受 syncPluginStyles 的
// 声明清单清理（该清理按 data-plugin-style 比对，皮肤 css 不在 client_styles
// 声明内——曾注入后一秒即被当失效样式摘除，2026-08-21 实锤）；生命周期
// 归主题切换（apply/clear 同一模块）
const STYLE_ID_ATTR = 'data-theme-style'
const PLUGIN_ATTR = 'data-plugin'

/** 是否为带皮肤资产的主题（声明驱动：任何插件声明 skin 字段即生效） */
export function isSkinTheme(
  theme: { skin?: string; pluginId?: string } | null | undefined,
): theme is PluginTheme & { skin: string } {
  return typeof theme?.skin === 'string' && theme.skin.length > 0 && !!theme.pluginId
}

/** scope 值（html[data-skin] 的属性值；全局唯一 = 插件:皮肤） */
function scopeValueOf(theme: PluginTheme & { skin: string }): string {
  return `${theme.pluginId}:${theme.skin}`
}

/** 摘除当前皮肤注入（属性 + <style> + hooks 清理 + 装饰层清空 + 槽位收回） */
function disposeCurrentSkin(): void {
  document.documentElement.removeAttribute('data-skin')
  document.body.removeAttribute('data-skin-dark')
  // 皮肤激活期挂的背景图信号摘除（与主题管线互斥 owner，见 applyPluginSkin）
  document.body.classList.remove('has-bg-image')
  document
    .querySelectorAll(`style[${STYLE_ID_ATTR}^="skin-"]`)
    .forEach((el) => el.remove())
  disposeSkinHooks()
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
    const url = `/ext/${theme.pluginId}/styles/skin/${theme.skin}/merged.css`
    const res = await apiClient.get<string>(url, {
      responseType: 'text',
      transformResponse: [(d) => d],
    })
    if (applyGeneration !== gen) return // 已被更新的切换取代
    const css = typeof res.data === 'string' ? res.data : String(res.data)
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
    if (applyGeneration === gen) reserveChromeStrips()
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

/**
 * 槽位预留（注入不能挤掉/遮住现有 UI）：只预留**带文字内容的替代性条栏**
 * （如 miku 标题栏/状态栏——它们顶替了被我们删掉的顶栏/状态栏，内容需要让位）。
 * 纯图形垂坠装饰（maid 顶/底花边：pointer-events none、内容从下方穿过的
 * 覆盖层）原生零位移——预留会把侧栏/工作区顶出空白带+页面被挤到中间
 * （真机实锤"比之前还差"）。判定=条带有非空文字内容。
 * 条带允许横向偏移（maid 顶花边 translate 避让侧栏 left≠0）。
 */
function reserveChromeStrips(): void {
  const rootStyle = document.documentElement.style
  let top = 0
  let bottom = 0
  const halfWidth = window.innerWidth / 2
  for (const el of document.body.children) {
    if (!(el instanceof HTMLElement)) continue
    const cs = getComputedStyle(el)
    if (cs.position !== 'fixed') continue
    const rect = el.getBoundingClientRect()
    if (rect.height <= 0 || rect.height > 80) continue
    if (rect.width < halfWidth) continue
    // 纯图形垂坠（无文字）不预留——原生覆盖式装饰
    if (!el.innerText.trim()) continue
    if (cs.top === '0px') {
      top = Math.max(top, Math.round(rect.height))
    } else if (cs.bottom === '0px') {
      bottom = Math.max(bottom, Math.round(rect.height))
    }
  }
  if (top > 0) rootStyle.setProperty('--skin-chrome-top', `${top}px`)
  else rootStyle.removeProperty('--skin-chrome-top')
  if (bottom > 0) rootStyle.setProperty('--skin-chrome-bottom', `${bottom}px`)
  else rootStyle.removeProperty('--skin-chrome-bottom')
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
      `/ext/${theme.pluginId}/styles/skin/${theme.skin}/hooks.mjs`,
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
      // （带尾斜杠拼出 // 路径，内核路由 404=背景图/立绘静默全灭，真机实锤）
      assetBase: `/ext/${theme.pluginId}/styles/skin-assets/${theme.skin}`,
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
      // apply 半途失败：皮肤在开头就注册了 onCleanup（实测），不回滚会留下
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
