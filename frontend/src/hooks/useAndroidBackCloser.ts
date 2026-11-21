/**
 * useAndroidBackCloser —— Android back 键浮层关闭器挂钩（P1-8）。
 *
 * active 期间注册关闭器：壳侧 back 事件到达时调用 close 并标记消费（壳不再
 * 回退/退出）。close 走 ref 转介（调用方无需 useCallback 稳定化），active 翻
 * 假自动注销（浮层关了注册即撤，不吞后续 back）。
 */

import { useEffect, useRef } from 'react'
import { registerAndroidBackCloser } from '@/services/androidBackBridge'

export function useAndroidBackCloser(active: boolean, close: () => void) {
  const closeRef = useRef(close)
  closeRef.current = close

  useEffect(() => {
    if (!active) return
    return registerAndroidBackCloser(() => {
      closeRef.current()
      return true
    })
  }, [active])
}
