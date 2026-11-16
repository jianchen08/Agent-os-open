/**
 * W 通用 widget 递送 — SDK 类型与构建约定（设计真值 =
 * docs/working/DSH生态嫁接通用机制设计_20261104.md §4.4「组件递送路线」W 主力）
 *
 * ## 产物约定（插件前端产物）
 *
 * 插件包携带按宿主 SDK 约定构建的前端产物，伺服于插件 ext 路由下：
 *
 * ```
 * <插件包>/frontend/main.js   ← 产物唯一约定路径（PLUGIN_WIDGET_BUNDLE_PATH）
 * ```
 *
 * 运行时宿主通用加载器（widgetLoader.ts）经 `/ext/<pluginId>/frontend/main.js`
 * 动态 import（同文档加载，非 iframe）。
 *
 * ## 构建约定
 *
 * - **external react/antd**：宿主库不进产物（React 版本契约冻结在宿主侧）；
 *   产物以全局 externals 引用宿主提供的 `window.React`（加载器装载前注入，
 *   esbuild 示例见 WIDGET_SDK_ESBUILD_CONFIG）。
 * - 产物模块形态：ESM，导出 **activate(api)**（或 default 同签名），
 *   装载时宿主调用一次：
 *
 * ```js
 * // frontend/main.js（构建产物入口源码示例）
 * export function activate(api) {
 *   api.registerWidget('status_light', StatusLight)   // 落表为 <pluginId>/status_light
 * }
 * ```
 *
 * - **注册 API 最小面**：`{registerWidget(name, Component), tokens, bridge}`——
 *   无全局对象/store/router，组件只见 props（DSH「组件不见 ctx」纪律同源，
 *   跨域只传 JSON+回调）。
 * - 主题 token 强制：产物内尺寸/颜色一律消费 `tokens` 下发的 CSS 变量值或
 *   主题 token 名，硬编码审出即拒。
 */

import type { ComponentType } from 'react'

/** 插件前端产物约定路径（相对插件根；伺服 URL = /ext/<pluginId>/frontend/main.js） */
export const PLUGIN_WIDGET_BUNDLE_PATH = '/frontend/main.js'

/** SDK 构建约定：external 清单（宿主库不进产物） */
export const WIDGET_SDK_ESBUILD_EXTERNALS = ['react', 'react-dom', 'antd'] as const

/**
 * SDK 参考构建配置（esbuild 形态；rollup 等价映射 external→globals 同理）。
 *
 * react 以全局 external 引用：宿主加载器在 import 产物前把 React 挂到
 * window（ensureHostGlobals），产物内 `import React from 'react'` 经 alias
 * 指向宿主全局，产物本体零宿主库拷贝。
 */
export const WIDGET_SDK_ESBUILD_CONFIG = {
  format: 'esm',
  bundle: true,
  external: WIDGET_SDK_ESBUILD_EXTERNALS,
} as const

/** 插件 bridge：跨域只传 JSON 的最小请求面（走宿主 API 封装，带鉴权） */
export interface PluginBridge {
  /** JSON 请求：path 相对 API 根（如 `/api/v1/...`），body 序列化为 JSON */
  request(path: string, init?: { method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'; body?: unknown }): Promise<unknown>
}

/** 插件前端产物收到的宿主 API（最小面，DSH「组件不见 ctx」纪律同源） */
export interface WidgetHostApi {
  /**
   * 注册 widget 组件。name 为插件内裸名（禁带 `/`）——宿主强制落到
   * `<pluginId>/<name>` 命名空间，插件不可注册其他命名空间。
   */
  registerWidget(name: string, component: ComponentType<Record<string, unknown>>): void
  /** 主题 token 快照（装载时解析，键 = CSS 变量名） */
  tokens: Record<string, string>
  /** JSON 请求桥 */
  bridge: PluginBridge
}

/** 插件前端产物模块约定形态 */
export interface WidgetBundleModule {
  /** 装载入口（default 导出同签名兼容） */
  activate?: (api: WidgetHostApi) => void
  default?: (api: WidgetHostApi) => void
}
