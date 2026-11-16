/**
 * 样例 widget 插件包前端产物入口源码（SDK 约定形态）
 *
 * 构建约定（widgetSdk.ts）：react/antd 为 external（不进产物），宿主加载器
 * 在 import 产物前把 React 挂到 window；本源码按经典 createElement 书写
 * （产物侧零 JSX 运行时依赖），由代表性测试通道按 WIDGET_SDK_ESBUILD_CONFIG
 * 构建（react 经全局 shim 别名，等价于生产 rollup external→globals 映射）。
 *
 * 注册 API 最小面：{registerWidget(name, Component), tokens, bridge}——
 * 无全局对象/store/router，组件只见 props。
 */

import React from 'react'

function StatusLight(props) {
  const state = props && props.state ? String(props.state) : 'idle'
  const label = props && props.label ? ' ' + String(props.label) : ''
  return React.createElement(
    'div',
    { 'data-testid': 'sample-status-light' },
    '状态灯:' + state + label,
  )
}

/** 渲染即抛错组件：ErrorBoundary 隔离行为验证用（同批注册，声明引用） */
function Boom() {
  throw new Error('sample_widget boom（代表性 ErrorBoundary 验证）')
}

function Announce(props) {
  const text = props && props.text ? String(props.text) : ''
  return React.createElement('div', { 'data-testid': 'sample-announce' }, '公告:' + text)
}

export function activate(api) {
  // 供测试断言注册 API 最小面形态（无全局对象/store/router 出口）
  globalThis.__sampleWidgetApiShape = Object.keys(api).sort()
  globalThis.__sampleWidgetTokenKeys = Object.keys(api.tokens ?? {})
  api.registerWidget('status_light', StatusLight)
  api.registerWidget('boom', Boom)
  api.registerWidget('announce', Announce)
}

export default activate
