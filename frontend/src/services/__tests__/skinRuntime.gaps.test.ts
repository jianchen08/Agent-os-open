/** @feature FP-0.2.四 前端Schema | @ci: frontend-test */
/**
 * skinRuntime 缺口补测：hooks 动态层的执行/回滚/清理链路 + pin 持久化失败兜底
 * + 指纹计算环境兜底（skinRuntime.test.ts / skinRuntime.hooks.test.ts /
 * skinRuntime.hookPin.test.ts 已覆盖静态注入面与确认流，此处专攻其未尽区域）。
 *
 * 观测手法：hooks 模块源码把事件推到 globalThis.__skinGapEvents（不依赖
 * 「复 import 同一 data: URL 读模块状态」——该路径在本环境因 ESM loader 缓存
 * 语义不可复现，见 skinRuntime.hooks.test.ts 头注），apply 的入参契约与
 * 清理/回滚副作用全部经事件通道在宿主侧断言。
 *
 * 环境适配：jsdom 无 blob: 资产层，URL.createObjectURL 打桩为等价 data: URL
 * （Node 原生 loader 可 import）；apiClient（网络传输）打桩。
 * sanitizeCss/getStyleNonce/hooks 模块本体全真实现。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { loggers } from '@/utils/logger'

const mockApiGet = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/client', () => ({
  apiClient: { get: (...args: unknown[]) => mockApiGet(...args) },
}))

import {
  applyPluginSkin,
  clearPluginSkin,
  saveSkinHookPin,
} from '../skinRuntime'
import { useSkinConsentStore } from '@/stores/skinConsentStore'

const OK_CSS = 'html[data-skin="p:ok"] .deco { position: absolute; top: 0; }'

/** 当前 hooks.mjs 源文本（apiClient 打桩与 createObjectURL 打桩共享同一真值） */
let hooksSource = ''

/** 与实现内 createObjectURL 产物同构的模块 URL（data: 版，Node loader 可导入） */
function hooksModuleUrl(source = hooksSource): string {
  return `data:text/javascript;charset=utf-8,${encodeURIComponent(source)}`
}

/** 与实现内 createObjectURL 打桩共享的事件通道（皮肤脚本 apply 副作用落点） */
interface GapEvent {
  kind: string
  [key: string]: unknown
}
let events: GapEvent[] = []

/** 预置 pin = 当前 hooksSource 的 sha256（绕过确认流、直行动态层的机制类前置） */
async function applyWithPin(theme: Parameters<typeof applyPluginSkin>[0]): Promise<void> {
  const scope = `${theme.pluginId}:${theme.skin}`
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(hooksSource))
  const hash = Array.from(new Uint8Array(digest))
    .map((b) => b.toString(16).padStart(2, '0'))
    .join('')
  saveSkinHookPin(scope, hash)
  await applyPluginSkin(theme)
}

function themeOf(skin: string, base: 'light' | 'dark' = 'light') {
  return { pluginId: 'p', id: `t-${skin}`, name: skin, base, skin } as never
}

function skinStyles(): HTMLStyleElement[] {
  return Array.from(document.querySelectorAll('style[data-theme-style^="skin-"]'))
}

function layersHost(): HTMLElement | null {
  return document.getElementById('skin-layers')
}

beforeEach(() => {
  events = []
  ;(globalThis as Record<string, unknown>).__skinGapEvents = events
  mockApiGet.mockReset()
  mockApiGet.mockImplementation(
    (url: unknown, config?: { transformResponse?: Array<(d: string) => string> }) => {
      const pass = (raw: string) => config?.transformResponse?.[0]?.(raw) ?? raw
      if (String(url).endsWith('hooks.mjs')) return Promise.resolve({ data: pass(hooksSource) })
      return Promise.resolve({ data: pass(OK_CSS) })
    },
  )
  vi.spyOn(URL, 'createObjectURL').mockImplementation(() => hooksModuleUrl())
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  hooksSource = ''
  localStorage.removeItem('agentos_skin_hook_pins')
  useSkinConsentStore.getState().setPending(null)
  clearPluginSkin()
})

afterEach(() => {
  clearPluginSkin()
  vi.restoreAllMocks()
  delete (globalThis as Record<string, unknown>).__skinGapEvents
  document.body.className = ''
  document.documentElement.removeAttribute('data-skin')
  document.documentElement.removeAttribute('style')
})

describe('hooks 执行链路：apply(ctx) 契约与六层宿主', () => {
  it('default 工厂：apply 收到完整契约 ctx（scope/assetBase/六层/z 序/theme 双底色）', async () => {
    hooksSource = `
      export default () => ({
        apply(ctx) {
          globalThis.__skinGapEvents.push({
            kind: 'applied',
            skinId: ctx.skinId,
            scopeAttr: ctx.scopeAttr,
            assetBase: ctx.assetBase,
            themeGet: ctx.theme.get(),
            subscribeType: typeof ctx.theme.subscribe(),
            layers: Object.fromEntries(Object.entries(ctx.layers).map(([k, el]) => [k, {
              attr: el.getAttribute('data-skin-layer'),
              z: el.style.zIndex,
              pe: el.style.pointerEvents,
            }])),
          })
          ctx.onCleanup(() => globalThis.__skinGapEvents.push({ kind: 'cleanup' }))
        },
      })
    `
    await applyWithPin(themeOf('miku', 'dark'))

    const applied = events.find((e) => e.kind === 'applied') as {
      skinId: string
      scopeAttr: string
      assetBase: string
      themeGet: string
      subscribeType: string
      layers: Record<string, { attr: string; z: string; pe: string }>
    }
    expect(applied).toBeDefined()
    expect(applied.skinId).toBe('miku')
    expect(applied.scopeAttr).toBe('p:miku')
    expect(applied.assetBase).toBe('/ext/p/styles/skin-assets/miku')
    expect(applied.themeGet).toBe('dark')
    expect(applied.subscribeType).toBe('function')

    // 六层齐备：名字锚点正确、z 序 background < ambient < sidebar < top < bottom < foreground
    const zOf = (name: string) => Number(applied.layers[name].z)
    for (const name of ['background', 'ambient', 'top', 'bottom', 'sidebar', 'foreground']) {
      expect(applied.layers[name].attr).toBe(name)
      expect(applied.layers[name].pe).toBe('none')
    }
    expect(zOf('background')).toBeLessThan(zOf('ambient'))
    expect(zOf('ambient')).toBeLessThan(zOf('sidebar'))
    expect(zOf('sidebar')).toBeLessThan(zOf('top'))
    expect(zOf('top')).toBeLessThan(zOf('bottom'))
    expect(zOf('bottom')).toBeLessThan(zOf('foreground'))

    // 层宿主常驻 body、fixed 全幅、pointer-events none
    const host = layersHost()
    expect(host).not.toBeNull()
    expect(host!.style.position).toBe('fixed')
    expect(host!.style.pointerEvents).toBe('none')
    expect(document.querySelectorAll('#skin-layers [data-skin-layer]')).toHaveLength(6)

    // 摘除：cleanup 执行 + 层内容清空（宿主保留）+ blob URL 回收
    const url = hooksModuleUrl()
    clearPluginSkin()
    expect(events.filter((e) => e.kind === 'cleanup')).toHaveLength(1)
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(url)
    for (const layer of Array.from(host!.children)) {
      expect(layer.children).toHaveLength(0)
    }
  })

  it('light 底色第二组输入：theme.get() 返回 light（与 dark 可区分）', async () => {
    hooksSource = `
      export default {
        apply(ctx) { globalThis.__skinGapEvents.push({ kind: 'applied', themeGet: ctx.theme.get() }) },
      }
    `
    await applyWithPin(themeOf('moe', 'light'))
    const applied = events.find((e) => e.kind === 'applied') as { themeGet: string }
    expect(applied.themeGet).toBe('light')
    expect(document.body.hasAttribute('data-skin-dark')).toBe(false)
  })

  it('default 直接出对象（非工厂）：同样执行 apply；工厂返回无 apply → 跳过不抛', async () => {
    // ① 直接对象：typeof api === 'function' 为假，不触发工厂调用
    hooksSource = `
      export default {
        apply(ctx) { globalThis.__skinGapEvents.push({ kind: 'applied', skinId: ctx.skinId }) },
      }
    `
    await applyWithPin(themeOf('direct'))
    const direct = events.find((e) => e.kind === 'applied') as { skinId: string } | undefined
    expect(direct?.skinId).toBe('direct')

    // ② 工厂返回空对象：无 apply 跳过，静态层照常
    events = []
    ;(globalThis as Record<string, unknown>).__skinGapEvents = events
    hooksSource = `export default () => { globalThis.__skinGapEvents.push({ kind: 'factory-called' }); return {} }`
    await applyWithPin(themeOf('noop'))
    expect(events.some((e) => e.kind === 'factory-called')).toBe(true)
    expect(events.some((e) => e.kind === 'applied')).toBe(false)
    expect(skinStyles()).toHaveLength(1)
    // 无 apply → 层宿主零装饰（宿主本体可能常驻自前一 apply，不新增内容）
    expect(document.querySelectorAll('#skin-layers [data-skin-layer] > *')).toHaveLength(0)
  })
})

describe('hooks 失败回滚与清理韧性', () => {
  it('apply 半途失败：已注册 cleanup 逆序回滚，静态层不受影响，摘除不重复执行', async () => {
    hooksSource = `
      export default () => ({
        apply(ctx) {
          globalThis.__skinGapEvents.push({ kind: 'cleanup-reg', id: 'a' })
          ctx.onCleanup(() => globalThis.__skinGapEvents.push({ kind: 'rollback', id: 'a' }))
          globalThis.__skinGapEvents.push({ kind: 'cleanup-reg', id: 'b' })
          ctx.onCleanup(() => globalThis.__skinGapEvents.push({ kind: 'rollback', id: 'b' }))
          throw new Error('apply 爆炸')
        },
      })
    `
    await expect(applyWithPin(themeOf('boom'))).resolves.toBeUndefined()
    const rolled = events.filter((e) => e.kind === 'rollback').map((e) => e.id as string)
    expect(rolled).toEqual(['b', 'a']) // 逆序
    expect(skinStyles()).toHaveLength(1) // 静态层照常
    // 已回滚的登记表清空：摘除不再重复执行
    clearPluginSkin()
    expect(events.filter((e) => e.kind === 'rollback')).toHaveLength(2)
  })

  it('摘除时 cleanup 自身抛异常：被吞掉，后续清理仍按逆序执行', async () => {
    hooksSource = `
      export default {
        apply(ctx) {
          ctx.onCleanup(() => globalThis.__skinGapEvents.push({ kind: 'cleanup', id: 'ok' }))
          ctx.onCleanup(() => {
            globalThis.__skinGapEvents.push({ kind: 'cleanup', id: 'throwing' })
            throw new Error('cleanup 爆炸')
          })
        },
      }
    `
    await applyWithPin(themeOf('messy'))
    expect(() => clearPluginSkin()).not.toThrow()
    const ids = events.filter((e) => e.kind === 'cleanup').map((e) => e.id as string)
    expect(ids).toEqual(['throwing', 'ok']) // 逆序且不因爆炸中断
  })
})

describe('竞态与层宿主重建', () => {
  it('import 期间被新切换取代：迟到的 hooks 不执行 apply，URL 回收，新皮肤落地', async () => {
    hooksSource = `
      await new Promise((resolve) => setTimeout(resolve, 60))
      export default () => ({ apply() { globalThis.__skinGapEvents.push({ kind: 'applied' }) } })
    `
    const slowUrl = hooksModuleUrl()
    const slowApply = applyWithPin(themeOf('slow'))
    await new Promise((resolve) => setTimeout(resolve, 20)) // 等 slow 进入 import（TLA 挂起）
    hooksSource = '   ' // 新皮肤无 hooks 动态层
    await applyWithPin(themeOf('fast'))
    await slowApply

    expect(events.some((e) => e.kind === 'applied')).toBe(false) // 代际复查拦下迟到者
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(slowUrl)
    const styles = skinStyles()
    expect(styles).toHaveLength(1)
    expect(styles[0]!.getAttribute('data-theme-style')).toBe('skin-p:fast')
  })

  it('层宿主被外部移除后再应用：重建宿主而非复用悬空引用，两次 apply 层都有效', async () => {
    hooksSource = `
      export default () => ({
        apply(ctx) {
          globalThis.__skinGapEvents.push({
            kind: 'applied',
            layerCount: Object.keys(ctx.layers).length,
            topAttr: ctx.layers.top.getAttribute('data-skin-layer'),
          })
        },
      })
    `
    await applyWithPin(themeOf('one'))
    expect(layersHost()).not.toBeNull()
    layersHost()!.remove()
    await applyWithPin(themeOf('two'))
    const counts = events.filter((e) => e.kind === 'applied').map((e) => e.layerCount as number)
    expect(counts).toEqual([6, 6])
    expect(document.querySelectorAll('#skin-layers [data-skin-layer]')).toHaveLength(6)
  })
})

describe('指纹与持久化兜底', () => {
  it('指纹计算环境不可用（digest 拒绝）→ 保守跳过动态层：不挂确认、静态层照常', async () => {
    const warnSpy = vi.spyOn(loggers.websocket, 'warn').mockImplementation(() => {})
    const digestSpy = vi
      .spyOn(crypto.subtle, 'digest')
      .mockRejectedValue(new Error('subtle unavailable'))
    hooksSource = `export default { apply() {} }`
    await expect(applyPluginSkin(themeOf('nosubtle'))).resolves.toBeUndefined()

    expect(digestSpy).toHaveBeenCalled()
    expect(useSkinConsentStore.getState().pending).toBeNull() // fail-closed：不执行也不挂卡
    expect(skinStyles()).toHaveLength(1) // 静态层不受影响
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('指纹计算失败'))
    expect(document.querySelectorAll('#skin-layers [data-skin-layer] > *')).toHaveLength(0)
  })

  it('pin 持久化失败（localStorage 写入抛出）→ 只记日志不外泄异常', () => {
    const warnSpy = vi.spyOn(loggers.websocket, 'warn').mockImplementation(() => {})
    const setItemSpy = vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('quota exceeded')
    })
    expect(() => saveSkinHookPin('a:1', 'h1')).not.toThrow()
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining('pin 持久化失败'))
    expect(localStorage.getItem('agentos_skin_hook_pins')).toBeNull() // 未落盘
    setItemSpy.mockRestore()
    // 恢复后写入照常（同函数第二组输入）
    expect(() => saveSkinHookPin('a:1', 'h2')).not.toThrow()
    expect(JSON.parse(localStorage.getItem('agentos_skin_hook_pins')!)).toEqual({ 'a:1': 'h2' })
  })
})
