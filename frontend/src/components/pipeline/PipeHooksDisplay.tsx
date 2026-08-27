/**
 * 管道钩子只读展示（管道步骤服务化提案 §3.6）。
 *
 * 展示 step 级 / 循环体级 hooks 声明（{on, run}）：on 徽标 + run 目标。
 * P1 只读——编辑器的 raw-preserving 语义保证未编辑的 hooks 键在保存时
 * 原样保留；hooks 的增删改走 yaml 直接编辑（编辑 UI 待需求定稿后再建）。
 */

import { Bell } from '@/assets/icons'
import { Badge } from '@/components/ui/badge'
import type { PipeHookEntry } from '@/services/pipeline/model'

export function PipeHooksDisplay({
  hooks,
  scopeHint,
}: {
  hooks: PipeHookEntry[] | undefined
  /** 作用域提示（如 step id），用于空态与 testid */
  scopeHint: string
}) {
  const list = Array.isArray(hooks) ? hooks : []
  if (list.length === 0) {
    return (
      <p className="text-muted-foreground text-[11px]" data-testid={`pipe-hooks-empty-${scopeHint}`}>
        无钩子声明
      </p>
    )
  }
  return (
    <ul className="flex flex-wrap gap-1.5" data-testid={`pipe-hooks-${scopeHint}`}>
      {list.map((hook, i) => {
        const run = typeof hook?.run === 'string' ? hook.run : ''
        const event = typeof hook?.on === 'string' ? hook.on : ''
        return (
          <li
            key={`${run}-${i}`}
            className="border-border bg-[var(--hover-overlay)] inline-flex max-w-full items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs"
            title={`钩子：事件 "${event}" 触发时通知 ${run}（fire-and-forget，最小作用域装载）`}
          >
            <Bell className="text-muted-foreground h-3 w-3 shrink-0" />
            <span className="text-muted-foreground shrink-0 font-mono text-[10px]">{event}</span>
            <span className="text-foreground truncate font-mono">{run}</span>
            <Badge variant="secondary" className="shrink-0 text-[9px]">
              hook
            </Badge>
          </li>
        )
      })}
    </ul>
  )
}
