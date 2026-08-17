/**
 * EventWatchBox — 声明组件事件联动外壳（widget 化 G3）
 *
 * 订阅一组事件（watch 规则）；事件触发后重挂载子组件（key 递增 + 注入
 * `reloadKey` prop）——对"数据在挂载时获取"的组件（如 FormWidget datasource
 * 模式重新 GET、未来数据 widget）即实现"提交后自动刷新"的联动语义。
 *
 * 用法（声明层）：
 *   { "id": "form_b", "type": "form",
 *     "props": { "watch": [{ "event": "task.created", "action": "reload" }] } }
 */
import { useEffect, useMemo, useState } from 'react'
import { subscribeFormEvent, type FormEventWatch } from '@/services/schema/formEventBus'
import type { ReactNode } from 'react'

export interface EventWatchBoxProps {
  /** watch 规则（单条或数组） */
  watch: FormEventWatch | FormEventWatch[]
  /** 渲染子组件（reloadKey 变化时重挂载） */
  children: (reloadKey: number) => ReactNode
}

export function EventWatchBox({ watch, children }: EventWatchBoxProps) {
  const rules = useMemo(() => (Array.isArray(watch) ? watch : [watch]), [watch])
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    const unsubscribers = rules.map((rule) =>
      subscribeFormEvent(rule.event, () => {
        if (rule.action === 'reload') setReloadKey((k) => k + 1)
      }),
    )
    return () => unsubscribers.forEach((unsub) => unsub())
  }, [rules])

  return <>{children(reloadKey)}</>
}
