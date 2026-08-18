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
 */
import { useEffect, useMemo, useState } from 'react'
import type { ReactNode } from 'react'

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

  useEffect(() => {
    const intervalMs = rules[0].intervalSeconds > 0 ? rules[0].intervalSeconds * 1000 : 0
    if (intervalMs === 0) return
    const timer = window.setInterval(() => setReloadKey((k) => k + 1), intervalMs)
    return () => window.clearInterval(timer)
  }, [rules])

  return <>{children(reloadKey)}</>
}
