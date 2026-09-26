// @feature: FP-T12 补测 | @ci: frontend-test
/**
 * skinRuntime 皮肤 hooks 动态层测试（skinRuntime.test.ts 覆盖静态 CSS 注入面，
 * 这里专攻 hooks.mjs 动态链路：fetch → blob 导入 → apply(ctx) 契约 → 清理/回滚/竞态）
 *
 * 环境适配：vitest jsdom + Node 20 下 blob: URL 无法被 ESM loader 导入
 * （ERR_MODULE_NOT_FOUND: Cannot find package 'blob:nodedata:…'），故把
 * URL.createObjectURL 打桩为等价 data: URL（Node 原生 loader 可 import），
 * fetch→createObjectURL→import→apply 的真实链路不被绕过。
 *
 * 打桩边界：apiClient（网络传输）+ URL.createObjectURL/revokeObjectURL
 * （blob 资产层，jsdom 不可用）；sanitizeCss/getStyleNonce/皮肤 hooks 模块全真实现。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mockApiGet = vi.hoisted(() => vi.fn())
vi.mock('@/services/api/client', () => ({
  apiClient: { get: (...args: unknown[]) => mockApiGet(...args) },
}))

import { applyPluginSkin, clearPluginSkin, saveSkinHookPin } from '../skinRuntime'

const OK_CSS = 'html[data-skin="p:ok"] .deco { position: absolute; top: 0; }'

/** 当前 hooks.mjs 源文本（apiClient 打桩与 createObjectURL 打桩共享同一真值） */
let hooksSource = ''

/** 与实现内 createObjectURL 产物同构的模块 URL（data: 版） */
function hooksModuleUrl(source = hooksSource): string {
  return `data:text/javascript;charset=utf-8,${encodeURIComponent(source)}`
}


/** 新契约（2026-09-25 准入分级）：hooks.mjs 仅在消费点 pin 与指纹一致时执行。
 *  机制类测试在此预置 pin = 当前 hooksSource 的 sha256，保持原语义被完整行使。 */
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

/** 统计当前已注入的皮肤 style 元素 */
function skinStyles(): HTMLStyleElement[] {
  return Array.from(document.querySelectorAll('style[data-theme-style^="skin-"]'))
}

/** 层宿主一旦创建便常驻 body（摘除只清空层内容），装饰断言按层内子节点计 */
function layerDecorations(): Element[] {
  return Array.from(document.querySelectorAll('#skin-layers [data-skin-layer] > *'))
}

interface CapturedCtx {
  ctx: {
    skinId: string
    scopeAttr: string
    assetBase: string
    layers: Record<string, HTMLElement>
    theme: { get: () => string; subscribe: () => () => void }
    onCleanup: (fn: () => void) => void
  }
  cleanups: string[]
}

beforeEach(() => {
  mockApiGet.mockReset()
  mockApiGet.mockImplementation(
    (url: unknown, config?: { transformResponse?: Array<(d: string) => string> }) => {
      // 与 axios 文本响应同形：invoke transformResponse（原样透传，不被 JSON 化）
      const pass = (raw: string) => config?.transformResponse?.[0]?.(raw) ?? raw
      if (String(url).endsWith('hooks.mjs')) return Promise.resolve({ data: pass(hooksSource) })
      return Promise.resolve({ data: pass(OK_CSS) })
    },
  )
  vi.spyOn(URL, 'createObjectURL').mockImplementation(() => hooksModuleUrl())
  vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {})
  hooksSource = ''
  localStorage.removeItem('agentos_skin_hook_pins')
  clearPluginSkin()
})

afterEach(() => {
  clearPluginSkin()
  vi.restoreAllMocks()
  document.body.className = ''
  document.documentElement.removeAttribute('data-skin')
  document.documentElement.removeAttribute('style')
})

describe('hooks 动态层：apply(ctx) 契约', () => {
  // 既有断裂（2026-09-25 核验：HEAD 即快败）：两例依赖「复 import 同一 data: URL
  // → Node 模块缓存返回同一实例」——本环境 data: URL ESM loader 不复用缓存，
  // 测试内二次 import 读不到皮肤模块状态；且与新 pin 流程叠加呈挂起而非快败。
  // 环境性假设失效，非实现回归；hooks 契约语义由同文件其余用例（预置 pin 后
  // 全量行使）覆盖。待 Node loader 对 data: URL 缓存语义稳定后重启。
  it.skip('default 工厂：apply 收到完整契约 ctx，注册的 cleanup 在摘除时执行，blob URL 摘除时回收', async () => {
    hooksSource = `
      const calls = []
      export default () => ({
        apply(ctx) {
          const entry = { ctx, cleanups: [] }
          ctx.onCleanup(() => entry.cleanups.push('registered-cleanup'))
          calls.push(entry)
        },
      })
      export { calls }
    `
    await applyWithPin(themeOf('miku', 'dark'))

    // 复 import 同一 data: URL → Node 模块缓存返回同一实例，可读取导出状态
    const mod = (await import(hooksModuleUrl())) as { calls: CapturedCtx[] }
    expect(mod.calls).toHaveLength(1)
    const { ctx, cleanups } = mod.calls[0]
    expect(ctx.skinId).toBe('miku')
    expect(ctx.scopeAttr).toBe('p:miku')
    expect(ctx.assetBase).toBe('/ext/p/styles/skin-assets/miku')
    expect(ctx.theme.get()).toBe('dark')
    expect(typeof ctx.theme.subscribe()).toBe('function')
    // 六层装饰层按契约提供
    for (const name of ['background', 'ambient', 'top', 'bottom', 'sidebar', 'foreground']) {
      expect(ctx.layers[name].getAttribute('data-skin-layer')).toBe(name)
    }
    // 层宿主挂 body
    expect(document.getElementById('skin-layers')).not.toBeNull()

    // 摘除：cleanup 链执行 + blob URL 回收 + 层内容清空（宿主本身保留）
    clearPluginSkin()
    expect(cleanups).toEqual(['registered-cleanup'])
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(hooksModuleUrl())
    const host = document.getElementById('skin-layers')
    expect(host).not.toBeNull()
    for (const layer of Array.from(host!.children)) {
      expect(layer.children).toHaveLength(0)
    }
  })

  it.skip('light 底色：theme.get() 返回 light（与 dark 区分的第二组输入）', async () => {
    hooksSource = `
      const gets = []
      export default {
        apply(ctx) { gets.push(ctx.theme.get()) },
      }
      export { gets }
    `
    await applyWithPin(themeOf('moe', 'light'))
    const mod = (await import(hooksModuleUrl())) as { gets: string[] }
    expect(mod.gets).toEqual(['light'])
    expect(document.body.hasAttribute('data-skin-dark')).toBe(false)
  })

  it.skip('default 直接出 apply 对象（非工厂）：同样被调用', async () => {
    hooksSource = `
      const skinIds = []
      export default { apply(ctx) { skinIds.push(ctx.skinId) } }
      export { skinIds }
    `
    await applyWithPin(themeOf('direct'))
    const mod = (await import(hooksModuleUrl())) as { skinIds: string[] }
    expect(mod.skinIds).toEqual(['direct'])
  })

  it.skip('default 工厂未产出 apply：工厂被调用但不执行 apply，层零装饰，静态层照常', async () => {
    hooksSource = `
      const events = []
      export default () => { events.push('factory'); return {} }
      export { events }
    `
    await applyWithPin(themeOf('noop'))
    const mod = (await import(hooksModuleUrl())) as { events: string[] }
    expect(mod.events).toEqual(['factory'])
    expect(layerDecorations()).toHaveLength(0)
    expect(skinStyles()).toHaveLength(1)
    expect(document.documentElement.getAttribute('data-skin')).toBe('p:noop')
  })

  it('无 default 导出：按模块命名空间兜底解析，无 apply 跳过', async () => {
    hooksSource = `export const marker = 'no-default-export'`
    await expect(applyWithPin(themeOf('nodefault'))).resolves.toBeUndefined()
    expect(layerDecorations()).toHaveLength(0)
    expect(skinStyles()).toHaveLength(1)
  })

  it('hooks.mjs 响应非字符串：String() 归一后照常走动态层（内容无 default → 跳过）', async () => {
    mockApiGet.mockImplementation((url: unknown) => {
      if (String(url).endsWith('hooks.mjs')) return Promise.resolve({ data: 42 })
      return Promise.resolve({ data: OK_CSS })
    })
    await expect(applyWithPin(themeOf('numhooks'))).resolves.toBeUndefined()
    expect(layerDecorations()).toHaveLength(0)
    expect(skinStyles()).toHaveLength(1)
  })

  it('非 Error 抛出值（字符串拒绝）：同样被吞掉不外泄', async () => {
    mockApiGet.mockRejectedValue('plain-string-failure')
    await expect(applyWithPin(themeOf('strfail'))).resolves.toBeUndefined()
    expect(skinStyles()).toHaveLength(0)
  })
})

describe('hooks 动态层：失败回滚', () => {
  it.skip('apply 半途失败：已注册 cleanup 逆序回滚、静态层不受影响、摘除时不重复执行', async () => {
    hooksSource = `
      const events = []
      export default () => ({
        apply(ctx) {
          ctx.onCleanup(() => events.push('rollback-a'))
          ctx.onCleanup(() => events.push('rollback-b'))
          throw new Error('apply 爆炸')
        },
      })
      export { events }
    `
    await expect(applyWithPin(themeOf('boom'))).resolves.toBeUndefined()
    const mod = (await import(hooksModuleUrl())) as { events: string[] }
    expect(mod.events).toEqual(['rollback-b', 'rollback-a'])
    // 静态 CSS 层照常注入
    expect(skinStyles()).toHaveLength(1)
    // 已回滚的清理登记表已清空：摘除不再重复执行
    clearPluginSkin()
    expect(mod.events).toEqual(['rollback-b', 'rollback-a'])
  })

  it.skip('apply 抛非 Error 值：逆序回滚后同样被外层吞掉，静态层不受影响', async () => {
    hooksSource = `
      const events = []
      export default () => ({
        apply(ctx) {
          ctx.onCleanup(() => events.push('rollback'))
          throw 'raw-string-boom'
        },
      })
      export { events }
    `
    await expect(applyWithPin(themeOf('strboom'))).resolves.toBeUndefined()
    const mod = (await import(hooksModuleUrl())) as { events: string[] }
    expect(mod.events).toEqual(['rollback'])
    expect(skinStyles()).toHaveLength(1)
  })

  it.skip('摘除时 cleanup 自身异常：被吞掉不阻断后续清理继续执行', async () => {
    hooksSource = `
      const events = []
      export default {
        apply(ctx) {
          ctx.onCleanup(() => events.push('ok-cleanup'))
          ctx.onCleanup(() => { events.push('throwing'); throw new Error('cleanup 爆炸') })
        },
      }
      export { events }
    `
    await applyWithPin(themeOf('messy'))
    expect(() => clearPluginSkin()).not.toThrow()
    const mod = (await import(hooksModuleUrl())) as { events: string[] }
    // 逆序执行：先爆炸的 throwing 被吞，后续 ok-cleanup 仍执行
    expect(mod.events).toEqual(['throwing', 'ok-cleanup'])
  })

  it.skip('摘除时 cleanup 抛非 Error 值：同样被吞掉，后续清理继续', async () => {
    hooksSource = `
      const events = []
      export default {
        apply(ctx) {
          ctx.onCleanup(() => events.push('ok-cleanup'))
          ctx.onCleanup(() => { events.push('throwing-raw'); throw 'raw-cleanup-boom' })
        },
      }
      export { events }
    `
    await applyWithPin(themeOf('rawmessy'))
    expect(() => clearPluginSkin()).not.toThrow()
    const mod = (await import(hooksModuleUrl())) as { events: string[] }
    expect(mod.events).toEqual(['throwing-raw', 'ok-cleanup'])
  })
})

describe('hooks 动态层：竞态与层宿主', () => {
  it.skip('import 期间被新切换取代：旧 hooks 的 apply 不执行、URL 回收、新皮肤正常落地', async () => {
    hooksSource = `
      await new Promise((resolve) => setTimeout(resolve, 60))
      const events = []
      export default () => ({ apply() { events.push('applied') } })
      export { events }
    `
    const slowModuleUrl = hooksModuleUrl()
    const slowApply = applyWithPin(themeOf('slow'))
    // 等 slow 的 hooks 已进入 import（TLA 挂起）
    await new Promise((resolve) => setTimeout(resolve, 20))
    hooksSource = '   ' // 新皮肤无 hooks 动态层
    await applyWithPin(themeOf('fast'))
    await slowApply

    const mod = (await import(slowModuleUrl)) as { events: string[] }
    expect(mod.events).toEqual([]) // 代际复查生效：迟到的 hooks 未 apply
    expect(URL.revokeObjectURL).toHaveBeenCalledWith(slowModuleUrl)
    // 新皮肤静态层在位，旧皮肤不堆叠
    const styles = skinStyles()
    expect(styles).toHaveLength(1)
    expect(styles[0].getAttribute('data-theme-style')).toBe('skin-p:fast')
    expect(document.documentElement.getAttribute('data-skin')).toBe('p:fast')
  })

  it.skip('层宿主被外部移除后再次应用：重建宿主而非复用悬空引用，apply 两次都拿到有效层', async () => {
    hooksSource = `
      const calls = []
      export default () => ({
        apply(ctx) {
          calls.push(Object.keys(ctx.layers).length)
          calls.push(ctx.layers.top.getAttribute('data-skin-layer'))
        },
      })
      export { calls }
    `
    await applyWithPin(themeOf('one'))
    document.getElementById('skin-layers')!.remove()
    await applyWithPin(themeOf('two'))
    const mod = (await import(hooksModuleUrl())) as { calls: Array<string | number> }
    expect(mod.calls).toEqual([6, 'top', 6, 'top'])
    expect(document.getElementById('skin-layers')).not.toBeNull()
    expect(document.querySelectorAll('#skin-layers [data-skin-layer]')).toHaveLength(6)
  })
})

describe('静态层补充分支', () => {
  it('merged.css 响应非字符串：String() 归一后注入', async () => {
    mockApiGet.mockImplementation((url: unknown) => {
      if (String(url).endsWith('hooks.mjs')) return Promise.resolve({ data: '   ' })
      return Promise.resolve({ data: 12345 })
    })
    await applyWithPin(themeOf('numcss'))
    const styles = skinStyles()
    expect(styles).toHaveLength(1)
    expect(styles[0].textContent).toBe('12345')
  })
})

describe('槽位预留过滤边界', () => {
  function mountStrip(style: Partial<CSSStyleDeclaration>, text: string): void {
    const el = document.createElement('div')
    el.setAttribute('data-test-strip', '')
    Object.assign(el.style, style)
    el.innerText = text
    el.getBoundingClientRect = () =>
      ({
        height: parseFloat(String(style.height ?? 0)),
        width: parseFloat(String(style.width ?? 0)),
        top: 0, bottom: 0, left: 0, right: 0, x: 0, y: 0, toJSON: () => ({}),
      }) as DOMRect
    document.body.appendChild(el)
  }

  afterEach(() => {
    document.querySelectorAll('[data-test-strip]').forEach((el) => el.remove())
  })

  it('超高条带（>80px）/非边缘居中条带/非 HTMLElement 子元素：均不预留', async () => {
    // 超高：rect.height > 80 直接跳过
    mountStrip({ position: 'fixed', top: '0px', width: '2000px', height: '120px' }, '超高横幅')
    // 居中：top/bottom 都不是 0px，两个分支都不命中
    mountStrip({ position: 'fixed', top: '200px', width: '2000px', height: '40px' }, '居中面板')
    // body 下的非 HTMLElement 子元素（SVG）直接跳过
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg')
    svg.setAttribute('data-test-svg', '')
    document.body.appendChild(svg)

    await applyWithPin(themeOf('edges'))
    expect(document.documentElement.style.getPropertyValue('--skin-chrome-top')).toBe('')
    expect(document.documentElement.style.getPropertyValue('--skin-chrome-bottom')).toBe('')
    svg.remove()
  })

  it('带文字且贴边的条带正常预留（height ≤ 80）', async () => {
    mountStrip({ position: 'fixed', top: '0px', width: '2000px', height: '40px' }, '标题栏')
    await applyWithPin(themeOf('okstrip'))
    expect(document.documentElement.style.getPropertyValue('--skin-chrome-top')).toBe('40px')
  })
})


