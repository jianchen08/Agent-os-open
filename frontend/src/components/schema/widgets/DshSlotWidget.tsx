/**
 * DshSlotWidget — DSH 插件组件挂载位（widget: 'dsh-slot'）。
 *
 * 渲染的是插件**自己的** React 组件：后端 client-ui 清单给出「哪个包在哪个
 * 槽位注册了什么」，本 widget 按 `props.bundle / props.slot / props.contributionId`
 * 装载该包的 client bundle（`applyClientBundle`）并把注册项原样挂进灵汐页面。
 *
 * 三种如实状态（不造占位内容）：
 * - 装载中：loading；
 * - 装载失败 / 缺服务：错误提示 + 缺失服务名（fail-visible，不白屏）；
 * - 装载成功但该槽位无注册项：明确说明插件未在此槽位注册组件。
 *
 * 槽位映射（DSH → 灵汐页面位）由后端 client_ui.SLOT_PAGE_MAP 决定；本组件
 * 只按页面声明渲染，不认识具体插件。
 */

import { createElement, useEffect, useState } from 'react'
import { applyClientBundle, type DshBundleRuntime, type DshSlotEntry } from '@/services/dshAdapter/clientHost'
import type { ComponentType, ReactElement } from 'react'

/** 页面声明透传的 props（contributes.pages[].props）。 */
export interface DshSlotWidgetProps {
  /** 已装 DSH 包名（含 @scope）。 */
  bundle?: string
  /** DSH 槽位名（如 sidebar.right.pane.tab）。 */
  slot?: string
  /** 指定注册项 id/key；缺省渲染该槽位全部注册项。 */
  contributionId?: string | null
}

interface SlotState {
  status: 'loading' | 'ready' | 'error'
  entries: DshSlotEntry[]
  /** 该 bundle apply 时的 locale 服务（按注册的命名空间给组件 props.t 取词） */
  locale: DshBundleRuntime['locale']
  message?: string
}

/**
 * 注册项 → React 元素：函数按组件渲染（hooks 合法），元素原样返回。
 *
 * DSH 标准 seat：注册声明了 `locale: <ns>` 的组件，其 props.t 由宿主按该
 * 命名空间绑定（词表来自插件自己 register 进来的字典，宿主不编造文案）。
 */
function toElement(entry: DshSlotEntry, locale: DshBundleRuntime['locale'], key: number): ReactElement | null {
  const { component } = entry
  if (typeof component === 'function') {
    const props: Record<string, unknown> = { key }
    if (entry.localeNs && locale) {
      props.t = (slotKey: string, params?: Record<string, unknown>) =>
        locale.t(entry.localeNs as string, slotKey, params)
    }
    return createElement(component as ComponentType<Record<string, unknown>>, props)
  }
  if (component && typeof component === 'object' && '$$typeof' in (component as Record<string, unknown>)) {
    return component as ReactElement
  }
  return null
}

export function DshSlotWidget({ bundle, slot, contributionId }: DshSlotWidgetProps) {
  const [state, setState] = useState<SlotState>({ status: 'loading', entries: [], locale: null })

  useEffect(() => {
    if (!bundle || !slot) {
      setState({ status: 'error', entries: [], locale: null, message: '页面声明缺 bundle / slot' })
      return
    }
    let alive = true
    setState({ status: 'loading', entries: [], locale: null })
    void applyClientBundle(bundle).then((runtime) => {
      if (!alive) return
      if (!runtime.applied) {
        setState({
          status: 'error',
          entries: [],
          locale: null,
          message: runtime.missingServices.filter(Boolean).join('；') || 'client bundle 装载失败',
        })
        return
      }
      const entries = runtime.entries.filter(
        (entry) => entry.name === slot && (!contributionId || entry.id === contributionId),
      )
      setState({ status: 'ready', entries, locale: runtime.locale })
    })
    return () => {
      alive = false
    }
  }, [bundle, slot, contributionId])

  if (state.status === 'loading') {
    return <div className="p-4 text-sm text-[var(--muted-foreground)]">正在装载插件组件（{bundle}）…</div>
  }
  if (state.status === 'error') {
    return (
      <div className="p-4 text-sm text-[var(--destructive)]">
        <div className="font-medium">插件组件未能装载：{bundle}</div>
        <div className="mt-1 text-xs opacity-80">{state.message}</div>
      </div>
    )
  }
  if (state.entries.length === 0) {
    return (
      <div className="p-4 text-sm text-[var(--muted-foreground)]">
        插件 {bundle} 未在槽位 {slot} 注册组件。
      </div>
    )
  }
  return (
    <div className="flex h-full min-h-0 flex-col overflow-auto" data-dsh-slot={slot} data-dsh-bundle={bundle}>
      {state.entries.map((entry, index) => toElement(entry, state.locale, index))}
    </div>
  )
}
