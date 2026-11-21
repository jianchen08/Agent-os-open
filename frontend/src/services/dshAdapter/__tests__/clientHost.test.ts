/** @feature FP-0.2.可观测性 dsh_adapter 插件（组件宿主） | @ci: frontend-test */
/**
 * DSH 客户端组件宿主测试：槽位服务语义 + 槽位页动态注册/注销 + 装载失败分支。
 *
 * 真实 bundle 执行（blob 脚本 + 插件组件渲染）由 dev 实测覆盖；本文件锁
 * 宿主侧契约：注册/排序/去重、缺服务如实报告、清单变化时页面增删。
 */
import { createElement } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'

vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn() },
}))

import { apiClient } from '@/services/api/client'
import {
  __resetClientHostForTests,
  __setBundleRunnerForTests,
  applyClientBundle,
  DshLocaleService,
  DshSlotService,
  loadClientUiManifest,
  setPreferredLocale,
  syncClientUiPages,
  type DshClientBundle,
} from '../clientHost'

const get = vi.mocked(apiClient.get)

/** window.__ModuleLoader__ 取用（applyClientBundle 已跑 ensureModuleLoader 后有效）。 */
function moduleLoader(): {
  load: (spec: { id: string; factory: (require: (name: string) => unknown) => Record<string, unknown> }) => void
} {
  const holder = window as unknown as {
    __ModuleLoader__?: { load: (spec: { id: string; factory: (require: (name: string) => unknown) => Record<string, unknown> }) => void }
  }
  if (!holder.__ModuleLoader__) throw new Error('__ModuleLoader__ 未初始化（applyClientBundle 未跑到 ensureModuleLoader）')
  return holder.__ModuleLoader__
}

function bundlePage(overrides: Partial<DshClientBundle> = {}): DshClientBundle {
  return {
    bundle: 'dsh-desktop-sidecar',
    version: '0.1.0',
    entry: 'lib/composer.js',
    entry_url: '/ext/dsh_adapter/client-bundle/dsh-desktop-sidecar/lib/composer.js',
    scanned: true,
    slots: [{ name: 'sidebar.right.pane.tab', id: 'steer', label: null, order: null }],
    injects: ['sidebar.right.pane.tab'],
    pages: [
      {
        id: 'dsh_ui_dsh_desktop_sidecar_sidebar_right_pane_tab',
        title: 'dsh-desktop-sidecar',
        space: 'workspace',
        slot: 'tab',
        widget: 'dsh-slot',
        props: { bundle: 'dsh-desktop-sidecar', slot: 'sidebar.right.pane.tab', contributionId: 'steer' },
      },
    ],
    unmapped: [],
    ...overrides,
  }
}

describe('DshSlotService', () => {
  it('inject 立即回调（槽位视为就绪）', () => {
    const slots = new DshSlotService()
    const cb = vi.fn()
    slots.inject('a', cb)
    expect(cb).toHaveBeenCalledTimes(1)
  })

  it('register 收 id/key 两形态并按 order 升序投影', () => {
    const slots = new DshSlotService()
    const A = () => null
    const B = () => null
    slots.register({ name: 's', id: 'b', order: 9 }, B)
    slots.register({ name: 's', key: 'a', order: 1 }, A)
    const entries = slots.entriesOf('s')
    expect(entries.map((e) => e.id)).toEqual(['a', 'b'])
    expect(slots.entriesOf('other')).toEqual([])
  })

  it('缺 name 或组件为空的注册被忽略', () => {
    const slots = new DshSlotService()
    slots.register({}, () => null)
    slots.register({ name: 's' }, undefined)
    expect(slots.all()).toEqual([])
  })

  it('缺 id/order/locale 声明 → 投影为 null 且 order 缺省排后', () => {
    const slots = new DshSlotService()
    const comp = () => null
    slots.register({ name: 's', order: 1 }, comp)
    slots.register({ name: 's' }, comp)
    slots.register({ name: 's', order: 2 }, comp)
    const entries = slots.entriesOf('s')
    expect(entries.map((e) => e.order)).toEqual([1, 2, null])
    expect(entries[2].id).toBeNull()
    expect(entries[2].localeNs).toBeNull()
  })

  it('locale 声明为字符串 → 投影为 localeNs', () => {
    const slots = new DshSlotService()
    slots.register({ name: 's', id: 'loc', locale: 'petcare' }, () => null)
    expect(slots.entriesOf('s')[0].localeNs).toBe('petcare')
  })
})

describe('DshLocaleService', () => {
  it('语言表契约：register({zh,en}) 后 bind 按 ns 取词', () => {
    const locale = new DshLocaleService()
    locale.register('pet', { zh: { 'tab.title': '桌宠', 'tab.hint': '点我' }, en: { 'tab.title': 'Pet' } })
    const t = locale.bind('pet')
    expect(t('tab.title')).toBe('桌宠')
    expect(t('tab.hint')).toBe('点我')
  })

  it('缺词回退键名；首选语言缺档回退 en', () => {
    const locale = new DshLocaleService()
    locale.register('ns', { en: { greet: 'hello {name}' } })
    const t = locale.bind('ns')
    expect(t('missing.key')).toBe('missing.key')
    expect(t('greet', { name: '灵' })).toBe('hello 灵')
  })

  it('重复注册按命名空间与语言合并', () => {
    const locale = new DshLocaleService()
    locale.register('ns', { zh: { a: '一' } })
    locale.register('ns', { zh: { b: '二' }, en: { a: 'one' } })
    const t = locale.bind('ns')
    expect([t('a'), t('b')]).toEqual(['一', '二'])
  })
})

describe('syncClientUiPages', () => {
  beforeEach(() => {
    __resetClientHostForTests()
    __setBundleRunnerForTests(vi.fn(async () => {}))
    contributionRegistry.clear()
    get.mockReset()
  })

  afterEach(() => {
    __setBundleRunnerForTests(null)
  })

  it('按清单注册槽位页，同签名重复同步不重注册', async () => {
    get.mockResolvedValue({ data: { bundles: [bundlePage()] } })
    const first = await syncClientUiPages()
    expect(first.registered).toBe(1)
    expect(contributionRegistry.getPage('dsh_ui_dsh_desktop_sidecar_sidebar_right_pane_tab')?.widget).toBe('dsh-slot')

    const second = await syncClientUiPages()
    expect(second).toEqual({ registered: 0, removed: 0 })
  })

  it('清单里消失的页被注销', async () => {
    get.mockResolvedValue({ data: { bundles: [bundlePage()] } })
    await syncClientUiPages()
    get.mockResolvedValue({ data: { bundles: [] } })
    const result = await syncClientUiPages()
    expect(result.removed).toBe(1)
    expect(contributionRegistry.getPage('dsh_ui_dsh_desktop_sidecar_sidebar_right_pane_tab')).toBeUndefined()
  })

  it('清单请求失败 → 空清单（不抛）', async () => {
    get.mockRejectedValue(new Error('network'))
    expect(await loadClientUiManifest()).toEqual([])
    expect(await syncClientUiPages()).toEqual({ registered: 0, removed: 0 })
  })

  it('清单响应缺 data.bundles → 空数组', async () => {
    get.mockResolvedValue({ data: undefined })
    expect(await loadClientUiManifest()).toEqual([])
  })

  it('页声明变化 → 重注册（removed 不计，registered 计 1）', async () => {
    get.mockResolvedValue({ data: { bundles: [bundlePage()] } })
    await syncClientUiPages()
    const changed = bundlePage()
    changed.pages[0].title = 'changed-title'
    get.mockResolvedValue({ data: { bundles: [changed] } })
    const result = await syncClientUiPages()
    expect(result).toEqual({ registered: 1, removed: 0 })
    expect(contributionRegistry.getPage('dsh_ui_dsh_desktop_sidecar_sidebar_right_pane_tab')?.title).toBe('changed-title')
  })

  it('防御性清理：注册表里 dsh_ui_ 前缀残留页（清单已无）被注销', async () => {
    // 模拟跨会话残留：注册表已有本前缀页但清单不再携带
    contributionRegistry.register({ type: 'pages', id: 'dsh_ui_stale_leftover', space: 'workspace', title: '残留页' })
    expect(contributionRegistry.getPage('dsh_ui_stale_leftover')).toBeDefined()
    get.mockResolvedValue({ data: { bundles: [] } })
    const result = await syncClientUiPages()
    expect(result).toEqual({ registered: 0, removed: 0 })
    expect(contributionRegistry.getPage('dsh_ui_stale_leftover')).toBeUndefined()
  })

  it('无页面 bundle 也全量 apply（DSH shell 启动语义：CSS/locale 类插件生效）', async () => {
    const pageless = bundlePage({
      bundle: 'dsh-always-status-bar',
      entry: 'lib/client.js',
      entry_url: '/ext/dsh_adapter/client-bundle/dsh-always-status-bar/lib/client.js',
      slots: [],
      injects: [],
      pages: [],
    })
    const runner = vi.fn(async () => {})
    __setBundleRunnerForTests(runner)
    get.mockImplementation(((url: string) => {
      if (String(url).includes('client-bundle/')) {
        return Promise.resolve({ data: 'window.__ModuleLoader__.load({id:"x",factory:()=>({})})' })
      }
      return Promise.resolve({ data: { bundles: [pageless] } })
    }) as typeof apiClient.get)
    await syncClientUiPages()
    // 无页面 bundle 的 client 入口也被拉取并执行（apply-all）
    expect(get).toHaveBeenCalledWith(
      '/ext/dsh_adapter/client-bundle/dsh-always-status-bar/lib/client.js',
      expect.objectContaining({ responseType: 'text' }),
    )
    expect(runner).toHaveBeenCalledWith(expect.stringContaining('__ModuleLoader__'), 'dsh-always-status-bar')
  })
})

describe('applyClientBundle 失败分支', () => {
  beforeEach(() => {
    __resetClientHostForTests()
    get.mockReset()
  })

  it('包不在清单里 → applied=false 且给出原因', async () => {
    get.mockResolvedValue({ data: { bundles: [] } })
    const runtime = await applyClientBundle('absent')
    expect(runtime.applied).toBe(false)
    expect(runtime.missingServices.join('')).toContain('未安装或未启用')
  })

  it('bundle 拉取失败 → applied=false 且不缓存失败结果', async () => {
    get.mockResolvedValueOnce({ data: { bundles: [bundlePage()] } })
    get.mockRejectedValueOnce(new Error('boom'))
    const runtime = await applyClientBundle('dsh-desktop-sidecar')
    expect(runtime.applied).toBe(false)
    expect(runtime.missingServices.join('')).toContain('装载失败')
  })
})

describe('ensureModuleLoader + require 映射（桩 runner 真实 load）', () => {
  beforeEach(() => {
    __resetClientHostForTests()
    get.mockReset()
  })

  afterEach(() => {
    __setBundleRunnerForTests(null)
  })

  it('load factory 执行 + require("react") 返回宿主 React（恒等）', async () => {
    let requiredReact: unknown
    __setBundleRunnerForTests(
      vi.fn(async (_source: string, bundle: string) => {
        moduleLoader().load({
          id: bundle,
          factory: (require) => {
            requiredReact = require('react')
            return { apply() {} }
          },
        })
      }),
    )
    get.mockResolvedValue({ data: { bundles: [bundlePage()] } })
    const runtime = await applyClientBundle('dsh-desktop-sidecar')
    expect(runtime.applied).toBe(true)
    // require('react') 命中宿主 React（同一实例，createElement 恒等）
    expect((requiredReact as { createElement?: unknown }).createElement).toBe(createElement)
    // 未提供的依赖如实拒绝
    __resetClientHostForTests()
    __setBundleRunnerForTests(
      vi.fn(async (_source: string, bundle: string) => {
        moduleLoader().load({
          id: bundle,
          factory: (require) => {
            require('left-pad')
            return { apply() {} }
          },
        })
      }),
    )
    const runtime2 = await applyClientBundle('dsh-desktop-sidecar')
    expect(runtime2.applied).toBe(false)
    expect(runtime2.missingServices.join('')).toContain('装载失败')
    expect(runtime2.missingServices.join('')).toContain('dsh client bundle 依赖未提供: left-pad')
  })
})

describe('runBundleScript（blob 脚本真身，jsdom 手动触发 onload/onerror）', () => {
  const createObjectURL = vi.fn(() => 'blob:dsh-test-url')
  const revokeObjectURL = vi.fn()

  beforeEach(() => {
    __resetClientHostForTests()
    __setBundleRunnerForTests(null)
    get.mockReset()
    // jsdom 对 blob URL 支持不保证，桩掉以聚焦脚本执行回调语义
    ;(URL as unknown as Record<string, unknown>).createObjectURL = createObjectURL
    ;(URL as unknown as Record<string, unknown>).revokeObjectURL = revokeObjectURL
    createObjectURL.mockClear()
    revokeObjectURL.mockClear()
  })

  afterEach(() => {
    vi.restoreAllMocks()
    __setBundleRunnerForTests(null)
  })

  it('onload 成功路：script 带 bundle 标记、blob URL 创建并回收', async () => {
    get.mockResolvedValue({ data: { bundles: [bundlePage()] } })
    const appended: HTMLScriptElement[] = []
    vi.spyOn(document.head, 'appendChild').mockImplementation(((node: Node) => {
      appended.push(node as HTMLScriptElement)
      ;(node as HTMLScriptElement).onload?.(new Event('load'))
      return node
    }) as typeof document.head.appendChild)

    const runtime = await applyClientBundle('dsh-desktop-sidecar')

    expect(createObjectURL).toHaveBeenCalledTimes(1)
    expect(appended).toHaveLength(1)
    expect(appended[0].getAttribute('src')).toBe('blob:dsh-test-url')
    expect(appended[0].dataset.dshBundle).toBe('dsh-desktop-sidecar')
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:dsh-test-url')
    // 脚本执行成功但模块未注册 apply → 如实报「模块未注册 apply」
    expect(runtime.applied).toBe(false)
    expect(runtime.missingServices.join('')).toContain('模块未注册 apply')
  })

  it('onerror 失败路 → 「(装载失败)」runtime 且 blob URL 仍回收', async () => {
    get.mockResolvedValue({ data: { bundles: [bundlePage()] } })
    vi.spyOn(document.head, 'appendChild').mockImplementation(((node: Node) => {
      ;(node as HTMLScriptElement).onerror?.(new Event('error'))
      return node
    }) as typeof document.head.appendChild)

    const runtime = await applyClientBundle('dsh-desktop-sidecar')

    expect(runtime.applied).toBe(false)
    expect(runtime.missingServices.join('')).toContain('(装载失败)')
    expect(runtime.missingServices.join('')).toContain('client bundle 脚本执行失败')
    expect(revokeObjectURL).toHaveBeenCalledTimes(1)
  })
})

describe('applyClientBundle 模块 apply 语义（桩 runner 真实注册模块）', () => {
  beforeEach(() => {
    __resetClientHostForTests()
    get.mockReset()
  })

  afterEach(() => {
    __setBundleRunnerForTests(null)
  })

  it('mod.inject 含未提供服务名 → applied=true 且 missingServices 如实上报', async () => {
    __setBundleRunnerForTests(
      vi.fn(async (_source: string, bundle: string) => {
        moduleLoader().load({
          id: bundle,
          factory: () => ({ inject: ['slots', 'locale', 'nope'], apply() {} }),
        })
      }),
    )
    get.mockResolvedValue({ data: { bundles: [bundlePage()] } })
    const runtime = await applyClientBundle('dsh-desktop-sidecar')
    expect(runtime.applied).toBe(true)
    expect(runtime.missingServices).toEqual(['nope'])
    expect(runtime.entries).toEqual([])
  })

  it('ctx 全语义：provide/get、effect(dispose 返回与解除)、inject 齐备回调/缺失收集、on/emit', async () => {
    const observed: Record<string, unknown> = {}
    let effectRan = false
    let injectCbRan = false
    let missingInjectCbRan = false
    __setBundleRunnerForTests(
      vi.fn(async (_source: string, bundle: string) => {
        moduleLoader().load({
          id: bundle,
          factory: () => ({
            apply(ctx: Record<string, unknown>) {
              ctx.provide('custom', 42)
              observed.custom = ctx.get('custom')
              const undo = ctx.effect(() => {
                effectRan = true
                return () => {}
              })
              expect(typeof undo).toBe('function')
              undo() // 解除 disposer 注册（不抛）
              undo() // 重复解除幂等
              // effect 返回非函数 → 不入 disposer 列表，解除仍安全
              const undoNoFn = ctx.effect(() => 'not-a-disposer')
              undoNoFn()
              ctx.inject(['slots', 'locale'], () => {
                injectCbRan = true
              })
              ctx.inject('nope', () => {
                missingInjectCbRan = true
              })
              const off = ctx.on('evt')
              expect(typeof off).toBe('function')
              ctx.emit('evt')
            },
          }),
        })
      }),
    )
    get.mockResolvedValue({ data: { bundles: [bundlePage()] } })
    const runtime = await applyClientBundle('dsh-desktop-sidecar')
    expect(runtime.applied).toBe(true)
    expect(observed.custom).toBe(42)
    expect(effectRan).toBe(true)
    expect(injectCbRan).toBe(true)
    expect(missingInjectCbRan).toBe(false)
    // inject('nope') 缺失收集进 runtime.missingServices
    expect(runtime.missingServices).toEqual(['nope'])
    expect(runtime.locale).toBeInstanceOf(DshLocaleService)
  })

  it('mod.apply 抛异常 → 「(apply 失败)」runtime 且失败不缓存', async () => {
    const runner = vi.fn(async (_source: string, bundle: string) => {
      moduleLoader().load({
        id: bundle,
        factory: () => ({
          apply() {
            throw new Error('apply boom')
          },
        }),
      })
    })
    __setBundleRunnerForTests(runner)
    get.mockResolvedValue({ data: { bundles: [bundlePage()] } })
    const runtime = await applyClientBundle('dsh-desktop-sidecar')
    expect(runtime.applied).toBe(false)
    expect(runtime.missingServices.join('')).toContain('(apply 失败)')
    expect(runtime.missingServices.join('')).toContain('apply boom')
    // apply 失败删除缓存：同包重试会重跑 runner
    await applyClientBundle('dsh-desktop-sidecar')
    expect(runner).toHaveBeenCalledTimes(2)
  })

  it('同包重复 apply → 命中缓存（同一 promise，幂等）', async () => {
    __setBundleRunnerForTests(vi.fn(async () => {}))
    get.mockResolvedValue({ data: { bundles: [bundlePage()] } })
    const first = applyClientBundle('dsh-desktop-sidecar')
    const second = applyClientBundle('dsh-desktop-sidecar')
    expect(second).toBe(first)
    await first
  })

  it('bundle 源码非字符串 → String() 化后交给 runner', async () => {
    const runner = vi.fn(async () => {})
    __setBundleRunnerForTests(runner)
    // 按 axios 契约模拟：entry 拉取带 transformResponse（恒等转换，防 JSON 解析），
    // 返回对象形态 data → 走 String() 分支
    get.mockImplementation(((url: string, config?: { transformResponse?: Array<(data: unknown) => unknown> }) => {
      if (String(url).includes('client-bundle/')) {
        const raw = { x: 1 }
        const transformed = config?.transformResponse?.[0]?.(raw) ?? raw
        return Promise.resolve({ data: transformed })
      }
      return Promise.resolve({ data: { bundles: [bundlePage()] } })
    }) as typeof apiClient.get)
    const runtime = await applyClientBundle('dsh-desktop-sidecar')
    expect(runner).toHaveBeenCalledWith('[object Object]', 'dsh-desktop-sidecar')
    // 模块未注册 → 如实报「模块未注册 apply」
    expect(runtime.applied).toBe(false)
    expect(runtime.missingServices.join('')).toContain('模块未注册 apply')
  })
})

describe('DshLocaleService 补充分支', () => {
  it('setPreferredLocale 生效：t 按首选语言取词（读回验证）', () => {
    const locale = new DshLocaleService()
    locale.register('ns', { en: { greet: 'hello' }, zh: { greet: '你好' } })
    setPreferredLocale('en')
    expect(locale.t('ns', 'greet')).toBe('hello')
    setPreferredLocale('zh')
    expect(locale.t('ns', 'greet')).toBe('你好')
  })

  it('全局读取抛异常 → 首选语言回退 zh（防御分支）', () => {
    const key = '__dshPreferredLocale'
    const original = Object.getOwnPropertyDescriptor(globalThis, key)
    Object.defineProperty(globalThis, key, {
      configurable: true,
      get: () => {
        throw new Error('global read blocked')
      },
      set: () => {},
    })
    try {
      const locale = new DshLocaleService()
      locale.register('ns', { zh: { greet: '你好' }, en: { greet: 'hello' } })
      // 读失败 → 回退 zh 取词，不抛
      expect(locale.t('ns', 'greet')).toBe('你好')
    } finally {
      if (original) Object.defineProperty(globalThis, key, original)
      else Reflect.deleteProperty(globalThis, key)
    }
  })

  it('非语言表（扁平 dict）→ 整体落 * 档，t 命中', () => {
    const locale = new DshLocaleService()
    locale.register('flat', { 'btn.ok': '好的', 'panel.title': '面板' })
    expect(locale.t('flat', 'btn.ok')).toBe('好的')
    expect(locale.t('flat', 'panel.title')).toBe('面板')
  })

  it('词表值含数组/数字 → 字符串化', () => {
    const locale = new DshLocaleService()
    locale.register('mix', { en: { count: 3, items: ['a', 'b'], nested: { deep: 'x' } } })
    expect(locale.t('mix', 'count')).toBe('3')
    expect(locale.t('mix', 'items')).toBe('a,b')
    expect(locale.t('mix', 'nested.deep')).toBe('x')
  })

  it('未注册命名空间回退键名；非对象词表不炸（null/数组/字符串）', () => {
    const locale = new DshLocaleService()
    expect(locale.t('absent', 'any.key')).toBe('any.key')
    locale.register('nullish', null)
    locale.register('arr', ['x'])
    locale.register('scalar', '文字')
    expect(locale.t('nullish', 'k')).toBe('k')
    expect(locale.t('arr', 'k')).toBe('k')
    expect(locale.t('scalar', 'k')).toBe('k')
  })

  it('模板参数缺失 → 占位保留原样', () => {
    const locale = new DshLocaleService()
    locale.register('ns', { zh: { greet: '你好 {name}' } })
    expect(locale.t('ns', 'greet', {})).toBe('你好 {name}')
    expect(locale.t('ns', 'greet', { name: '灵', extra: 1 })).toBe('你好 灵')
  })
})
