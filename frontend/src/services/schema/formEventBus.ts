/**
 * 轻量表单事件总线（widget 化 G3：声明组件间事件联动）
 *
 * 用途：表单/声明组件间的事件发布订阅——FormWidget 提交成功/失败发事件，
 * 其他组件（经 DeclaredWidgetLayer 的 watch 声明）订阅后触发重载/联动。
 *
 * 语义：
 * - 事件名由声明方给定（FormWidget props.eventName）；
 * - 提交成功 emit `{eventName}`，payload = 表单值；
 * - 提交失败 emit `{eventName}:failed`，payload = { error, values }。
 *
 * 被否方案（[来源: docs/decisions/2026-08-17-widget-event-linkage.md]）：
 * - DOM CustomEvent 全局广播：需序列化 payload、与 React 生命周期割裂、难测；
 * - context 广播：只能父→子，声明组件跨空间（chat-input 槽 vs workspace 槽）不可达。
 * 模块级发布订阅是最小可行：同构声明、可测、组件卸载自动退订。
 */

type Handler = (payload: unknown) => void

const listeners = new Map<string, Set<Handler>>()

/** 发布事件（所有订阅者同步收到 payload） */
export function emitFormEvent(name: string, payload?: unknown): void {
  const set = listeners.get(name)
  if (!set) return
  for (const handler of set) {
    try {
      handler(payload)
    } catch (error) {
      // 订阅者异常不中断其他订阅者
      console.error(`[formEventBus] 订阅者处理事件 ${name} 异常:`, error)
    }
  }
}

/**
 * 订阅事件。
 *
 * @returns 退订函数（组件卸载时调用；重复调用幂等）
 */
export function subscribeFormEvent(name: string, handler: Handler): () => void {
  let set = listeners.get(name)
  if (!set) {
    set = new Set()
    listeners.set(name, set)
  }
  set.add(handler)
  return () => {
    set?.delete(handler)
    if (set && set.size === 0) listeners.delete(name)
  }
}

/** watch 声明：事件触发后对组件执行的动作（目前支持 reload=重挂载重拉） */
export interface FormEventWatch {
  event: string
  action: 'reload'
}
