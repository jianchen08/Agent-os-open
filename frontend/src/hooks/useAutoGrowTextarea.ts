/**
 * textarea 自适应增高（2026-11-05 R4 件①共通 hook，禁消费点各自复制）。
 *
 * 行为：value 变化（手动输入与外部写入同路）→ height=auto → scrollHeight
 * 定高，maxHeight 封顶。消费点：FullscreenComposer 正文（上限 160=max-h-40）、
 * 引用浮条评语框（上限 96=max-h-24）。
 */

import { useEffect, useRef } from 'react'

export interface AutoGrowOptions {
  /** 高度封顶（px）：scrollHeight 超过时高度锁定为该值（内部滚动） */
  maxHeight: number
}

/** 受控 textarea 自适应增高：返回绑到 textarea 的 ref，高度随 value 重算 */
export function useAutoGrowTextarea<T extends HTMLTextAreaElement>(
  value: string,
  options: AutoGrowOptions,
): React.RefObject<T | null> {
  const ref = useRef<T | null>(null)

  useEffect(() => {
    const el = ref.current
    if (!el) return
    // height=auto 让浏览器按内容回排本征高，再以 scrollHeight 定格
    // （上限封顶：超出部分内部滚动，CSS max-h-* 类与该值保持一致）
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, options.maxHeight)}px`
  }, [value, options.maxHeight])

  return ref
}
