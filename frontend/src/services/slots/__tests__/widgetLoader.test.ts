/** @feature FP-T12 W 通用 widget 递送 | @ci: frontend-test */
/**
 * W 通用 widget 加载器测试（设计真值 = docs/working/DSH生态嫁接通用机制设计_20261104.md §4.4 W 机制链）
 *
 * 契约：
 * - 注册 API 最小面 {registerWidget(name, Component), tokens, bridge}：
 *   无全局对象/store/router，组件只见 props
 * - registerWidget 强制命名空间：<pluginId>/<name>（插件逃不出自己的命名空间）
 * - tokens：装载时快照主题 token 值下发
 * - bridge：JSON 请求面（request），跨域只传 JSON
 * - 动态装载走注入 importer（真实 dynamic import 在代表性测试通道验证）
 * - 装载失败不抛出：返回错误（SlotRegistry 据此 fail-closed 跳过条目）
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import {
  extractWidgetNamesFor,
  isNamespacedWidgetName,
  loadPluginWidgets,
} from '../widgetLoader'
import { PLUGIN_WIDGET_BUNDLE_PATH, WIDGET_SDK_ESBUILD_EXTERNALS } from '../widgetSdk'
import type { WidgetHostApi } from '../widgetSdk'
import type React from 'react'

const apiClientMock = { get: vi.fn(), request: vi.fn() }
vi.mock('@/services/api/client', () => ({
  apiClient: {
    get: (...a: unknown[]) => apiClientMock.get(...(a as [string])),
    request: (...a: unknown[]) => apiClientMock.request(...(a as [unknown])),
  },
}))

beforeEach(() => {
  widgetRegistry.clear()
})

describe('widgetSdk 构建约定', () => {
  it('SDK 约定：react/antd 为 external（产物不打包宿主库）', () => {
    expect(WIDGET_SDK_ESBUILD_EXTERNALS).toContain('react')
    expect(WIDGET_SDK_ESBUILD_EXTERNALS).toContain('antd')
  })

  it('SDK 约定：插件前端产物路径固定（约定优于配置，伺服于 /ext/<pluginId> 下）', () => {
    expect(PLUGIN_WIDGET_BUNDLE_PATH).toBe('/frontend/main.js')
  })
})

describe('命名空间', () => {
  it('isNamespacedWidgetName：仅 <pluginId>/<name> 形态为真', () => {
    expect(isNamespacedWidgetName('myplug/status_light')).toBe(true)
    expect(isNamespacedWidgetName('status_card')).toBe(false)
    expect(isNamespacedWidgetName('/leading')).toBe(false)
    expect(isNamespacedWidgetName('a/b/c')).toBe(false)
    expect(isNamespacedWidgetName('')).toBe(false)
  })

  it('extractWidgetNamesFor：取该插件命名空间内的 widget 名集合', () => {
    expect(extractWidgetNamesFor(['a/w1', 'a/w2', 'b/w1', 'builtin'], 'a')).toEqual(['a/w1', 'a/w2'])
  })
})

describe('createWidgetHostApi（注册 API 最小面）', () => {
  it('registerWidget 强制 <pluginId>/<name> 命名空间；非法名拒绝', async () => {
    const { createWidgetHostApi } = await import('../widgetLoader')
    const hostApi = createWidgetHostApi('myplug', { tokens: {}, bridge: { request: vi.fn() } })
    const Comp = () => null
    hostApi.registerWidget('status_light', Comp)
    // 命名空间强制：插件声明裸名 → 落表为 <pluginId>/<name>（动态源）
    expect(widgetRegistry.has('myplug/status_light')).toBe(true)
    expect(widgetRegistry.getEntry('myplug/status_light')?.metadata.source).toBe('plugin')
    // 试图自带前缀逃逸（注册他插件命名空间）→ 拒绝（fail-closed）
    expect(() => hostApi.registerWidget('otherplug/evil', Comp)).toThrow()
    // 空名拒绝
    expect(() => hostApi.registerWidget('', Comp)).toThrow()
    // 非组件拒绝
    expect(() => hostApi.registerWidget('not_a_component', 42 as never)).toThrow()
  })

  it('tokens 下发主题 token 快照（组件只见 props/tokens，不见 store）', async () => {
    const { createWidgetHostApi } = await import('../widgetLoader')
    const hostApi = createWidgetHostApi('myplug', {
      tokens: { '--ds-accent-primary': '#5b8def' },
      bridge: { request: vi.fn() },
    })
    expect(hostApi.tokens['--ds-accent-primary']).toBe('#5b8def')
    // 最小面：只暴露三个键（无全局对象/store/router 出口）
    expect(Object.keys(hostApi).sort()).toEqual(['bridge', 'registerWidget', 'tokens'])
  })
})

describe('loadPluginWidgets（W 动态装载链）', () => {
  it('经 importer 装载产物模块并执行注册：widget 落 widgetRegistry 动态命名空间', async () => {
    const Comp: React.FC<Record<string, unknown>> = () => null
    const importer = vi.fn().mockResolvedValue({
      // SDK 产物形态：导出 activate(api)（或 default 同签名）
      activate: (api: WidgetHostApi) => {
        api.registerWidget('status_light', Comp)
      },
    })
    await loadPluginWidgets('myplug', { importBundle: importer })
    expect(importer).toHaveBeenCalledWith('myplug')
    expect(widgetRegistry.has('myplug/status_light')).toBe(true)
    expect(widgetRegistry.getEntry('myplug/status_light')?.metadata.source).toBe('plugin')
  })

  it('产物 default 导出同签名兼容', async () => {
    const Comp = () => null
    await loadPluginWidgets('myplug', {
      importBundle: vi.fn().mockResolvedValue({
        default: (api: WidgetHostApi) => api.registerWidget('w', Comp),
      }),
    })
    expect(widgetRegistry.has('myplug/w')).toBe(true)
  })

  it('产物无 activate/default 导出 → 返回装载错误', async () => {
    const errs = await loadPluginWidgets('myplug', {
      importBundle: vi.fn().mockResolvedValue({ somethingElse: 1 }),
    })
    expect(errs).toHaveLength(1)
    expect(errs[0].reason).toContain('activate')
  })

  it('importer 抛错（网络/伺服 404）→ 返回装载错误不抛出（fail-closed 依据）', async () => {
    const errs = await loadPluginWidgets('myplug', {
      importBundle: vi.fn().mockRejectedValue(new Error('404 not found')),
    })
    expect(errs).toHaveLength(1)
    expect(errs[0].reason).toContain('404')
    expect(widgetRegistry.size).toBe(0)
  })

  it('默认产物装载：按 /ext/<pluginId>/frontend/main.js 动态导入（产物不可达 → 装载错误不抛）', async () => {
    // 不注入 importer = 走真实动态导入面；jsdom 无 /ext 伺服产物 → 拒绝 → fail-closed
    const errs = await loadPluginWidgets('probe_plugin')
    expect(errs).toHaveLength(1)
    expect(errs[0].pluginId).toBe('probe_plugin')
    expect(errs[0].reason).toContain('装载失败')
  })

  it('activate 内抛错 → 返回装载错误，已注册 widget 保留（部分成功可见）', async () => {
    const Comp = () => null
    const errs = await loadPluginWidgets('myplug', {
      importBundle: vi.fn().mockResolvedValue({
        activate: (api: WidgetHostApi) => {
          api.registerWidget('good', Comp)
          throw new Error('boom in activate')
        },
      }),
    })
    expect(errs).toHaveLength(1)
    expect(widgetRegistry.has('myplug/good')).toBe(true)
  })

  it('bridge.request：GET/无 body 走 apiClient.get；带 body 写方法走 apiClient.request', async () => {
    apiClientMock.get.mockResolvedValue({ ok: 1 })
    apiClientMock.request.mockResolvedValue({ written: 2 })
    // activate 不被装载器 await：把激活体包成 promise 由用例统一等待
    let done: Promise<void> = Promise.resolve()
    const seen: unknown[] = []
    await loadPluginWidgets('myplug', {
      importBundle: vi.fn().mockResolvedValue({
        activate: (api: WidgetHostApi) => {
          done = (async () => {
            seen.push(await api.bridge.request('/api/v1/things'))
            // POST 无 body：body === undefined 仍按读路径处理
            seen.push(await api.bridge.request('/api/v1/things', { method: 'POST' }))
            seen.push(
              await api.bridge.request('/api/v1/things', {
                method: 'PUT',
                body: { size: 3 },
              }),
            )
          })()
        },
      }),
    })
    await done
    expect(seen).toEqual([{ ok: 1 }, { ok: 1 }, { written: 2 }])
    expect(apiClientMock.get).toHaveBeenCalledTimes(2)
    expect(apiClientMock.get).toHaveBeenNthCalledWith(1, '/api/v1/things')
    expect(apiClientMock.get).toHaveBeenNthCalledWith(2, '/api/v1/things')
    expect(apiClientMock.request).toHaveBeenCalledTimes(1)
    expect(apiClientMock.request).toHaveBeenCalledWith({
      url: '/api/v1/things',
      method: 'PUT',
      data: { size: 3 },
    })
  })
})
