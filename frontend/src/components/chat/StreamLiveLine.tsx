/**
 * StreamLiveLine · 折叠态实时活动行（新增功能，2026-11-18 用户裁定）
 *
 * 折叠卡片在流式输出期间只占一行，实时展示「正在输出的内容」，无需展开即见进度。
 * 形态取 DSH 与 ZCode 实物实现的交集（对照规格见
 * docs/working/3a19e4183b20_折叠态流式展示对照规格.md）：
 *
 * - 取文（运行中）= **最后一行非空文本**
 *   ZCode `cTn`：按 `\r\n?/` 归一后 split('\n')，从末尾回溯首个 `trim()` 非空行；
 *   DSH `latestLine`：`trimEnd()` 后取 `lastIndexOf('\n')` 之后。两家同口径，采纳。
 * - 取文（终态）= **首行非空文本**（DSH `firstLine`；ZCode 终态整体不渲染预览行）。
 *   采纳 DSH：终态保留一行摘要信息量更高，且与本仓既有 subtitle 摘要形态一致。
 * - 横向跟随：运行中 `scrollLeft = scrollWidth - clientWidth`（滚到尾部），
 *   终态 `scrollLeft = 0`（回到行首）——ZCode `oTn` + DSH `data-follow-end` 同口径。
 * - 溢出渐隐：内容超宽时两端各 16px 渐隐遮罩（ZCode `uTn` 的
 *   `linear-gradient(to right, transparent 0, black 16px, black calc(100% - 16px), transparent 100%)`）。
 *
 * 单行形态（`whitespace-nowrap` + `overflow-hidden`）在桌面与移动端一致，
 * 不设独立移动分支（两参照源均无运行时移动分支）。
 *
 * 2026-11-19 用户纠正（三条）：①思考区不显示任何耗时数字；②思考内容只占一行，
 * 不得铺成多行段落（上一轮误加的 block 多行变体已删除）；③「只显示一半」是
 * **宽度**问题——调用方不得用 max-w-[N%] 或 flex 占位 div 压窄本行，须让它
 * flex-1 min-w-0 吃满可用宽度后再按容器宽度截断。
 */

import { useEffect, useRef, useState } from 'react'
import { cn } from '@/lib/utils'
import { firstLine, latestLine } from './streamLiveLineText'
import type { FC } from 'react'

/** 两端渐隐遮罩（ZCode uTn 同款：溢出时首尾各 16px 透明） */
const FADE_MASK =
  'linear-gradient(to right, transparent 0, black 16px, black calc(100% - 16px), transparent 100%)'

export interface StreamLiveLineProps {
  /** 全量流式文本（思考内容 / 工具执行输出拼接） */
  text: string
  /** true=运行中（跟随尾部，取最后一行）；false=终态（取首行并回到行首） */
  running: boolean
  className?: string
  testId?: string
}

/**
 * 折叠态实时活动行：运行中显示最后一行并横向滚尾，终态显示首行并回行首。
 * 文本全空时不渲染（由调用方决定是否留占位）。
 */
export const StreamLiveLine: FC<StreamLiveLineProps> = ({
  text,
  running,
  className,
  testId,
}) => {
  const lineRef = useRef<HTMLSpanElement>(null)
  const [overflowing, setOverflowing] = useState(false)
  const summary = running ? latestLine(text) : firstLine(text)

  useEffect(() => {
    const el = lineRef.current
    if (!el) return
    const measure = () => {
      // 横向跟随：运行中贴到尾部，终态回行首
      el.scrollLeft = running ? el.scrollWidth - el.clientWidth : 0
      const next = el.scrollWidth > el.clientWidth + 1
      setOverflowing((prev) => (prev === next ? prev : next))
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [summary, running])


  if (!summary) return null

  return (
    <span
      ref={lineRef}
      data-testid={testId}
      data-follow-end={running || undefined}
      className={cn(
        'text-muted-foreground/70 min-w-0 flex-1 overflow-hidden font-mono text-[11px] whitespace-nowrap',
        className,
      )}
      // eslint-disable-next-line no-restricted-syntax -- 条件生效的静态渐隐遮罩组（含 WebkitMask* 前缀族，Tailwind 任意值写法过长失读）；动态开关由 overflowing 承载，值本身是常量非计算值（编码纪律动态值例外）
      style={
        overflowing
          ? {
              WebkitMaskImage: FADE_MASK,
              maskImage: FADE_MASK,
              WebkitMaskRepeat: 'no-repeat',
              maskRepeat: 'no-repeat',
              WebkitMaskSize: '100% 100%',
              maskSize: '100% 100%',
            }
          : undefined
      }
      title={summary}
    >
      <span className="inline-block min-w-max">{summary}</span>
    </span>
  )
}

export default StreamLiveLine
