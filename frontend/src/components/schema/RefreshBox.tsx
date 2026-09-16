/**
 * RefreshBox — 声明式定时刷新外壳（widget 化 G6-b）
 *
 * 与 EventWatchBox 同构（事件驱动 ↔ 计时驱动）：按 refresh 声明周期重挂载
 * 子组件（key 递增 + 注入 reloadKey）——对"数据在挂载时获取"的组件即
 * 实时轮询刷新语义（FormWidget datasource 模式重新 GET 等）。
 *
 * 声明层用法：
 *   { "id": "status", "type": "form",
 *     "props": { "refresh": { "type": "poll", "intervalSeconds": 5 } } }
 *
 * 离屏暂停：本组件渲染的包裹 div 上判定可见性（工作区面板非激活 tab 为
 * display:none / 窗口最小化）——不可见时冻结轮询定时器（隐藏面板继续轮询
 * 只产出永不显示的重挂载与请求负载），恢复可见立即补拉一次（不等下一周期）。
 * intervalSeconds=0（无轮询声明）不参与暂停/补拉，行为与旧版一致。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { useElementVisible } from '@/hooks/useElementVisible'

export interface RefreshDecl {
  type: 'poll'
  intervalSeconds: number
}

export interface RefreshBoxProps {
  refresh: RefreshDecl
  children: (reloadKey: number) => ReactNode
}

export function RefreshBox({ refresh, children }: RefreshBoxProps) {
  const rules = useMemo(() => [refresh], [refresh])
  const [reloadKey, setReloadKey] = useState(0)
  const { ref, visible } = useElementVisible<HTMLDivElement>()
  const polling = rules[0].intervalSeconds > 0

  useEffect(() => {
    if (!visible || !polling) return
    const timer = window.setInterval(
      () => setReloadKey((k) => k + 1),
      rules[0].intervalSeconds * 1000,
    )
    return () => window.clearInterval(timer)
  }, [rules, visible, polling])

  /** 恢复可见立即重拉一次（仅轮询声明方有"数据新鲜度"语义需要补拉） */
  const wasVisible = useRef(true)
  useEffect(() => {
    const resumed = visible && !wasVisible.current
    wasVisible.current = visible
    if (resumed && polling) setReloadKey((k) => k + 1)
  }, [visible, polling])

  return <div ref={ref}>{children(reloadKey)}</div>
}
