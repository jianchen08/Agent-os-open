/**
 * useFadeOverflow —— 文字溢出「渐变遮蔽」收边
 *
 * 顶带标签契约（用户裁定 2026-09-21）：标签默认保持固定宽度上限，超长标题不在
 * 按钮内出现省略号，而是**用渐变遮蔽把尾部淡出**（视觉上仍是「这块还有内容」）；
 * 带宽被挤压时标签再缩小、少显示一部分文字。
 *
 * 实现：CSS `mask-image` 线性渐变盖在文字节点尾部，遮蔽只在真实溢出时出现
 * （不溢出时元素与普通文字无差别）。
 *
 * 遮蔽宽度用**百分比**表达而非 calc 像素：渐变端点位置天然按元素宽度缩放，
 * 标签越窄遮蔽越短，无需 JS 计算（jsdom 的 CSSStyleDeclaration 会拒收含
 * calc 的 mask 值，百分比形式在测试与运行时行为一致）。
 *
 * 单个 ResizeObserver 负责溢出判定：顶带列宽随分栏拖拽/开关变化，元素宽度变化
 * 不触发窗口 resize。
 */

import { useCallback, useLayoutEffect, useState } from 'react'

/** 遮蔽渐变的最大宽度（px）：宽标签封顶，避免大半文字被淡出 */
const FADE_MAX_PX = 20
/** 遮蔽渐变的最小宽度（px）：再窄也要能看出「尾部还有内容」 */
const FADE_MIN_PX = 8
/** 遮蔽宽度占可见宽度的目标比例 */
const FADE_TARGET_RATIO = 0.12
/** 溢出判定容差（px）：亚像素误差不触发遮蔽 */
const OVERFLOW_EPSILON = 1

/** 遮蔽宽度（px）：随可见宽度增长并封顶（导出供样式与测试共用同一算式） */
export function fadeWidthFor(visiblePx: number): number {
  return Math.min(FADE_MAX_PX, Math.max(FADE_MIN_PX, Math.round(visiblePx * FADE_TARGET_RATIO)))
}

/** 遮蔽样式：渐变端点在「可见宽度 - 遮蔽宽度」处开始淡出 */
export function fadeGradientFor(visiblePx: number): string {
  const ratio = visiblePx > 0 ? fadeWidthFor(visiblePx) / visiblePx : FADE_TARGET_RATIO
  const stop = Math.round((1 - ratio) * 100)
  return `linear-gradient(to right, black ${stop}%, transparent 100%)`
}

export interface FadeOverflow {
  /** 挂到文字宿主元素上的 ref（元素应是内容溢出的容器） */
  ref: React.RefCallback<HTMLElement>
  /** 是否溢出（供调用方做提示/埋点） */
  overflowing: boolean
  /** 宿主当前可见宽度（px；未挂载/无布局时为 0） */
  visibleWidth: number
}

export function useFadeOverflow(): FadeOverflow {
  const [el, setEl] = useState<HTMLElement | null>(null)
  const [overflowing, setOverflowing] = useState(false)
  const [visibleWidth, setVisibleWidth] = useState(0)

  /** 量一次：溢出判定 + 记录可见宽度 + 按结果落地/撤除遮蔽 */
  const measure = useCallback((node: HTMLElement | null) => {
    if (!node) return
    const width = node.clientWidth
    const isOverflowing = node.scrollWidth - width > OVERFLOW_EPSILON
    setOverflowing(isOverflowing)
    setVisibleWidth(width)
    if (isOverflowing) {
      const gradient = fadeGradientFor(width)
      node.style.setProperty('mask-image', gradient)
      node.style.setProperty('-webkit-mask-image', gradient)
    } else {
      node.style.removeProperty('mask-image')
      node.style.removeProperty('-webkit-mask-image')
    }
  }, [])

  const ref = useCallback(
    (node: HTMLElement | null) => {
      setEl(node)
      measure(node)
    },
    [measure],
  )

  useLayoutEffect(() => {
    if (!el) return
    // 首帧后复量：字体回填/布局时序会让首次测量偏小；jsdom 下 ResizeObserver 不回调，
    // 这条 rAF 是唯一能纠正首帧误差的路径
    const raf = requestAnimationFrame(() => measure(el))
    const observer = new ResizeObserver(() => measure(el))
    observer.observe(el)
    return () => {
      cancelAnimationFrame(raf)
      observer.disconnect()
    }
  }, [el, measure])

  return { ref, overflowing, visibleWidth }
}
