/**
 * W 通用 widget 加载器（设计真值 = docs/working/DSH生态嫁接通用机制设计_20261104.md
 * §4.4「组件递送路线」W 主力）
 *
 * 机制链：插件包携带按宿主 SDK 约定构建的产物（external react/antd，约定路径
 * frontend/main.js）→ 伺服于 /ext/<pluginId>/… → 宿主通用加载器 dynamic import
 * 同文档加载，传入注册 API `{registerWidget(name, Component), tokens, bridge}`
 * （最小面：无全局对象/store/router，组件只见 props）→ widgetRegistry 双源
 * （静态内建白名单 + 动态插件命名空间 `<pluginId>/<name>`）→ 装载期校验
 * 声明↔注册一致（SlotRegistry fail-closed，AC-F5）。
 *
 * 安全收敛（如实：第三方 JS 进宿主文档，较 webview 弱一档）：安装审批卡强制、
 * 命名空间强制（本模块 createWidgetHostApi）、伺服域同源（/ext 路由）。
 */

import React from 'react'
import { extUrl } from '@/services/api/extRoute'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import {
  PLUGIN_WIDGET_BUNDLE_PATH,
  type WidgetBundleModule,
  type WidgetHostApi,
} from './widgetSdk'
import type { SlotLoadError } from './slotRegistry'
import type { ComponentType } from 'react'

/** 插件命名空间 widget 名：`<pluginId>/<name>`（两侧各一段，禁多级） */
const NAMESPACED_WIDGET_RE = /^[A-Za-z0-9_-]+\/[A-Za-z0-9_-]+$/

export function isNamespacedWidgetName(name: string): boolean {
  return NAMESPACED_WIDGET_RE.test(name)
}

/** 取属于某插件命名空间的 widget 名集合（装载期一致性校验用） */
export function extractWidgetNamesFor(names: string[], pluginId: string): string[] {
  const prefix = `${pluginId}/`
  return names.filter((n) => n.startsWith(prefix) && isNamespacedWidgetName(n))
}

/**
 * 宿主全局注入：SDK 产物以 external react 构建（window.React 引用宿主实例）。
 * 装载前调用一次；已注入不覆盖（不 clobber 宿主/他方实例）。
 */
function ensureHostGlobals(): void {
  if (typeof window === 'undefined') return
  const w = window as unknown as Record<string, unknown>
  if (!w.React) w.React = React
}

/**
 * 构造插件侧收到的注册 API（最小面，强制命名空间）。
 *
 * registerWidget 只收插件内裸名（禁 `/`）——落表恒为 `<pluginId>/<name>`，
 * 插件逃不出自己的命名空间；组件/名非法直接抛错（fail-closed）。
 */
export function createWidgetHostApi(
  pluginId: string,
  deps: { tokens: Record<string, string>; bridge: WidgetHostApi['bridge'] },
): WidgetHostApi {
  return {
    registerWidget(name: string, component: ComponentType<Record<string, unknown>>): void {
      if (typeof name !== 'string' || name.trim() === '') {
        throw new Error(`[widgetLoader] widget 名不能为空（plugin=${pluginId}）`)
      }
      if (name.includes('/')) {
        throw new Error(
          `[widgetLoader] widget 名禁带 "/"（命名空间由宿主强制：<pluginId>/<name>），收到 "${name}"（plugin=${pluginId}）`,
        )
      }
      if (typeof component !== 'function') {
        throw new Error(`[widgetLoader] widget "${name}" 的 component 必须是函数组件（plugin=${pluginId}）`)
      }
      const qualified = `${pluginId}/${name}`
      widgetRegistry.register(qualified, component, {
        name: qualified,
        supportedSpaces: ['chat', 'workspace', 'floating'],
        source: 'plugin',
      })
    },
    tokens: deps.tokens,
    bridge: deps.bridge,
  }
}

/** 装载依赖缝：默认真实 dynamic import（伺服 URL），测试可注入 */
export interface WidgetLoaderDeps {
  /** 按插件 id 装载其前端产物模块（默认 /ext/<pluginId>/frontend/main.js） */
  importBundle?: (pluginId: string) => Promise<WidgetBundleModule>
}

/** 默认产物装载：同文档 dynamic import 插件 ext 路由下的约定路径 */
function defaultImportBundle(pluginId: string): Promise<WidgetBundleModule> {
  const url = extUrl(pluginId, PLUGIN_WIDGET_BUNDLE_PATH)
  return import(/* @vite-ignore */ url) as Promise<WidgetBundleModule>
}

function themeTokenSnapshot(): Record<string, string> {
  if (typeof window === 'undefined' || typeof document === 'undefined') return {}
  const styles = getComputedStyle(document.documentElement)
  const names = ['--ds-accent-primary', '--ds-bg-elevated', '--ds-border-active', '--border', '--muted', '--foreground', '--background']
  const out: Record<string, string> = {}
  for (const n of names) {
    const v = styles.getPropertyValue(n).trim()
    if (v) out[n] = v
  }
  return out
}

/**
 * 装载一个插件的前端产物并执行注册。
 *
 * 失败不抛出（返回 SlotLoadError 列表）——调用方（SlotRegistry）据此对引用
 * 该插件命名空间 widget 的槽声明做装载期报错跳过（AC-F5 fail-closed）。
 * activate 半途抛错时已注册的 widget 保留（部分成功可见，错误如实记录）。
 */
export async function loadPluginWidgets(
  pluginId: string,
  deps?: WidgetLoaderDeps,
): Promise<SlotLoadError[]> {
  const importBundle = deps?.importBundle ?? defaultImportBundle
  try {
    ensureHostGlobals()
    const mod = await importBundle(pluginId)
    const activate = mod?.activate ?? mod?.default
    if (typeof activate !== 'function') {
      return [{
        pluginId,
        reason: `widget 产物缺少 activate/default 导出（约定见 widgetSdk.ts，路径 ${PLUGIN_WIDGET_BUNDLE_PATH}）`,
      }]
    }
    const api = createWidgetHostApi(pluginId, {
      tokens: themeTokenSnapshot(),
      bridge: {
        async request(path, init) {
          const { apiClient } = await import('@/services/api/client')
          const method = init?.method ?? 'GET'
          if (method === 'GET' || init?.body === undefined) {
            return apiClient.get(path)
          }
          // AxiosInstance.request(config)：单配置参（写方法带 body）
          return apiClient.request({ url: path, method, data: init.body })
        },
      },
    })
    activate(api)
    return []
  } catch (err) {
    return [{
      pluginId,
      reason: `widget 产物装载失败：${err instanceof Error ? err.message : String(err)}`,
    }]
  }
}
