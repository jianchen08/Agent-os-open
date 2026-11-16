/**
 * 气泡选区引用浮层（2026-11-05 R4 件③，消息流接入）。
 *
 * 消费方式：容器（气泡）挂 onMouseUp=handleBubbleMouseUp；bar 非空时渲染
 * CitationFloatBar（positioning='fixed'，视口坐标）。
 *
 * 快照语义：文本与坐标在浮层弹出时定格（选区文本须在动作触发时仍可用的
 * 定格快照——动作读快照，后续选区塌陷不影响）。动作后由消费方 closeBar +
 * removeAllRanges。
 *
 * 关闭守卫（打开期间）：点击别处（pointerdown 落在浮条外，含 portal 判定）/
 * 滚动（capture 捕获任意容器滚动）；输入框区域豁免（R7 Bug① 统一语义——浮条
 * 活跃期点输入框保选区保浮条，utils/inputBoxTarget 判定）。Esc 关闭由
 * CitationFloatBar 自担。
 * 唯一浮层：同一气泡内由单份 bar state 保证；跨气泡=新选区的 pointerdown
 * 已把旧气泡浮层关掉后再弹新的。
 */

import { useCallback, useEffect, useState } from 'react'
import { closestInputBoxFromEvent } from '@/utils/inputBoxTarget'
import type { RefObject } from 'react'

/** 浮层快照：选区文本定格 + 视口坐标（fixed 定位） */
export interface CitationBarSnapshot {
  selectedText: string
  x: number
  y: number
}

/** 浮条顶部钳位（px）：贴视口顶时防浮层（translate -100% 后）整体越出视口 */
const FLOAT_BAR_MIN_TOP = 56

export function useBubbleCitationBar(
  containerRef: RefObject<HTMLElement | null>,
  enabled: boolean,
) {
  const [bar, setBar] = useState<CitationBarSnapshot | null>(null)

  const handleBubbleMouseUp = useCallback(() => {
    if (!enabled) return
    const selection = window.getSelection()
    if (!selection || selection.isCollapsed) return
    const selectedText = selection.toString().trim()
    if (!selectedText) return
    const container = containerRef.current
    if (!container) return
    const range = selection.getRangeAt(0)
    // 只认本气泡内的选区（range 起止端点都在容器内）——跨气泡选择不弹浮层，
    // 防多浮条并存。用 range 端点而非 anchorNode（选区跨节点时语义等价且
    // 与测试桩的 fakeSelection 字段一致）
    if (!container.contains(range.startContainer) || !container.contains(range.endContainer)) {
      return
    }
    const rect = range.getBoundingClientRect()
    setBar({
      selectedText,
      x: rect.left + rect.width / 2,
      y: Math.max(rect.top - 8, FLOAT_BAR_MIN_TOP),
    })
  }, [containerRef, enabled])

  const closeBar = useCallback(() => setBar(null), [])

  // 打开期间：点击别处 / 滚动 → 关闭。浮条自身（含 fixed portal）内的按下
  // 不关——「引用/评审」动作与评语输入都要在浮层存续下完成。
  useEffect(() => {
    if (!bar) return
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target
      // target 可能是浮条根节点本身或其子元素：Element 用 closest 自查，
      // 非元素目标（如文本节点）取父元素再查
      const el = target instanceof Element ? target : target instanceof Node ? target.parentElement : null
      if (el?.closest('[data-testid="citation-float-bar"]')) return
      // 输入框区域豁免（R7 Bug① 统一语义）：浮条活跃期点击输入框不关浮条
      // （选区高亮由 CitationFloatBar 的 mousedown 保护区保持），点「引用」照常入列
      if (closestInputBoxFromEvent(e)) return
      closeBar()
    }
    const onScroll = () => closeBar()
    document.addEventListener('pointerdown', onPointerDown, true)
    document.addEventListener('scroll', onScroll, true)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown, true)
      document.removeEventListener('scroll', onScroll, true)
    }
  }, [bar, closeBar])

  return { bar, closeBar, handleBubbleMouseUp }
}
