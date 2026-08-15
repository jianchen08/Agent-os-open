/**
 * resync_required 事件消费（G8 优雅重启的前端追赶腿 + G3 动态注册的被动感知通道）
 *
 * 内核在 WS 重连回放缓冲 miss 时下发 {"type": "resync_required"}（GlobalWebSocket.ts
 * onmessage 中 _emit('resync_required', data)，全文消息作为负载）。本模块为其消费端：
 *
 *   resync_required → 2s 防抖 → refreshPluginContributions()（GrowthLoop 既有重载链：
 *   getSchema → ContributionRegistry.loadFromSchema → 导航/快捷键/插件样式全刷新）
 *
 * 剩余项清仓 D2 扩展：schema 变更主动推送通道——内核在 schema 聚合变化点
 * （插件 enable/disable、G3 registry.register_tool 动态注册成功）best-effort 广播
 * widget_event {widget_id:"schema", event:"changed"}（经 SessionCoordinator
 * broadcast_widget，信封 {type:"widget_event", data:{widget_id, event, data}}）。
 * 本模块订阅 widget 事件流并过滤该消息，触发与 resync_required 完全相同的
 * 重载链（复用 _performResync + 同一防抖窗口，两个通道天然合并去抖）：
 *
 *   widget_event(schema, changed) → 同一防抖 → refreshPluginContributions()
 *
 * 设计要点：
 * - 防抖：重连风暴中事件可能连续到达（且 GlobalWebSocket 对同一消息会显式+按 type
 *   各 emit 一次，单条消息即两次），防抖窗口内合并为一次重载。
 * - 重入保护：重载进行中再次触发直接跳过，避免并发 loadFromSchema 交叉清理。
 * - 失败静默：拉取失败仅 warn 不抛出（不阻塞 WS 主流程），下次事件再试。
 * - 幂等订阅：handler 为模块级稳定引用，globalWS.subscribe 内部 Set 去重，重复 init
 *   无副作用；登出 disconnect() 清空 handler 后重新 init 也能正确补挂。
 * - 最小侵入：不经过 useWidgetEvents/widgetEventStore（那是 per-widget 渲染面，
 *   事件进 store 后还要再订阅 zustand）——直接订阅 globalWS 的 widget_event
 *   事件流，用 MessageAdapter.adaptWidgetEvent 解析（与 useWidgetEvents 同一解析器）。
 *
 * @module websocket/resync
 */

import { WS_SERVER_EVENTS } from '@/constants/websocket'
import { loggers } from '@/utils/logger'
import { adaptWidgetEvent } from './MessageAdapter'
import { globalWS } from './GlobalWebSocket'

const _wsLogger = loggers.websocket

/** 防抖窗口（毫秒）：重连风暴中连续 resync_required 合并为一次全量重同步 */
const RESYNC_DEBOUNCE_MS = 2_000

/** schema 变更推送的 widget_id（与内核 routes.rs / capability_router.rs 推送端约定） */
const SCHEMA_WIDGET_ID = 'schema'
/** schema 变更推送的事件名 */
const SCHEMA_CHANGED_EVENT = 'changed'

/** 待执行的防抖计时器 */
let _pendingTimer: ReturnType<typeof setTimeout> | null = null

/** 是否有重同步正在执行（重入保护） */
let _running = false

/**
 * 执行全量重同步：复用 GrowthLoop 的 reloadContributionRegistry 重载链
 * （经导出口 refreshPluginContributions 调用）。
 *
 * 动态 import 避免静态循环依赖：GrowthLoop 会静态导入本模块做接线，
 * 本模块运行时再反向加载 GrowthLoop。
 */
async function _performResync(): Promise<void> {
  if (_running) return
  _running = true
  try {
    _wsLogger.info('[resync] resync_required 触发 schema 全量重同步')
    const { refreshPluginContributions } = await import('@/services/modules/GrowthLoop')
    await refreshPluginContributions()
    _wsLogger.info('[resync] schema 全量重同步完成')
  } catch (error) {
    // 失败仅告警不抛出：不阻塞 WS 主流程，等待下次 resync_required 再试
    _wsLogger.warn('[resync] schema 重同步失败（等待下次 resync_required 再试）:', error)
  } finally {
    _running = false
  }
}

/** resync_required 事件处理：进入防抖窗口，窗口结束后执行一次重同步 */
function _handleResyncRequired(): void {
  // 防抖窗口内重复到达（含同一消息的双重 emit）直接合并
  if (_pendingTimer) return
  _pendingTimer = setTimeout(() => {
    _pendingTimer = null
    void _performResync()
  }, RESYNC_DEBOUNCE_MS)
}

/**
 * widget_event(schema, changed) 处理：过滤出内核 schema 变更推送，
 * 触发与 resync_required 相同的重载链（共用防抖窗口，两通道事件合并去抖）。
 */
function _handleSchemaWidgetEvent(raw: unknown): void {
  const adapted = adaptWidgetEvent(raw as Parameters<typeof adaptWidgetEvent>[0])
  if (!adapted) return
  if (adapted.widget_id !== SCHEMA_WIDGET_ID || adapted.event !== SCHEMA_CHANGED_EVENT) return
  _wsLogger.info('[resync] schema changed 推送到达，触发 schema 重同步')
  _handleResyncRequired()
}

/**
 * 初始化 resync 订阅（GrowthLoop 初始化/重启链上调用）。
 *
 * 幂等：handler 是模块级稳定引用，subscribe 内部 Set.add 去重；登出时
 * globalWS.disconnect() 清空全部 handler，重登后再次调用即可补挂。
 */
export function initResyncOnSchema(): void {
  globalWS.subscribe(WS_SERVER_EVENTS.RESYNC_REQUIRED, _handleResyncRequired)
  globalWS.subscribe(WS_SERVER_EVENTS.WIDGET_EVENT, _handleSchemaWidgetEvent)
}

/** 注销订阅并取消未触发的防抖（登出销毁闭环时调用，防止登出后仍触发重载） */
export function disposeResyncOnSchema(): void {
  globalWS.unsubscribe(WS_SERVER_EVENTS.RESYNC_REQUIRED, _handleResyncRequired)
  globalWS.unsubscribe(WS_SERVER_EVENTS.WIDGET_EVENT, _handleSchemaWidgetEvent)
  if (_pendingTimer) {
    clearTimeout(_pendingTimer)
    _pendingTimer = null
  }
}
