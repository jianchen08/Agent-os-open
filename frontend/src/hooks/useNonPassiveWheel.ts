/**
 * useNonPassiveWheel Hook
 *
 * 在目标元素上以「非被动」方式注册原生 wheel 事件。
 *
 * 背景: React 在根容器上把 onWheel 以 passive 形式绑定，因而在 onWheel 回调里
 *      调用 preventDefault() 不会生效，浏览器会告警
 *      "Unable to preventDefault inside passive event listener invocation"。
 *      本 hook 返回一个 ref callback，直接以 { passive: false } 在元素上注册
 *      原生 wheel 监听，使 preventDefault() 可以正常工作
 *      （例如把纵向滚轮转为横向滚动，或在 Lightbox 中拦截滚轮做缩放）。
 *
 * 用法:
 * ```tsx
 * const ref = useNonPassiveWheel<HTMLDivElement>((e) => {
 *   e.preventDefault()
 *   // ...
 * })
 * return <div ref={ref}>...</div>
 * ```
 *
 * 说明: 返回的是 ref callback，元素挂载时绑定、卸载时自动解绑，
 *      因此对条件渲染的元素同样适用。
 */

import { useCallback, useEffect, useRef } from 'react'

export function useNonPassiveWheel<T extends HTMLElement>(
  onWheel: (e: WheelEvent) => void,
) {
  // 用 ref 持有最新回调，避免回调变动时反复重新绑定监听
  const callbackRef = useRef(onWheel)
  callbackRef.current = onWheel

  // 记录当前绑定的解绑函数，供切换/卸载时清理
  const cleanupRef = useRef<(() => void) | null>(null)

  const refCallback = useCallback((el: T | null) => {
    cleanupRef.current?.()
    cleanupRef.current = null
    if (!el) return
    const handler = (e: WheelEvent) => callbackRef.current(e)
    el.addEventListener('wheel', handler, { passive: false })
    cleanupRef.current = () => el.removeEventListener('wheel', handler)
  }, [])

  // 组件卸载时兜底清理
  useEffect(
    () => () => {
      cleanupRef.current?.()
    },
    [],
  )

  return refCallback
}
