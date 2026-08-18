/**
 * WidgetStage — 声明 widget 演示台宿主（widget_demo 插件的舞台）。
 *
 * 渲染指定 space 的全部声明 widget（DeclaredWidgetLayer 附加式），并演示
 * G4 受控双向绑定桥：对声明 id 匹配的控件注入 value/onChange（宿主状态
 * 持有），覆盖 compact 受控（demo_controlled）、拖拽排序（demo_sortable）、
 * 内联编辑（demo_inline）三种形态——证明受控桥在非 chat-input 空间通用。
 *
 * 通用性：任何插件都可声明 contributes.pages {widget: 'widget_stage',
 * props: {space: 'xxx'}} 把一组声明 widget 摆到一个工作区页里。
 */
import { useState } from 'react'
import { DeclaredWidgetLayer } from '@/components/schema/DeclaredWidgetLayer'
import { useControlledSlotBridge } from '@/hooks/useControlledSlotBridge'
import type { WidgetDeclaration } from '@/services/schema/ContributionRegistry'

export function WidgetStage(props: Record<string, unknown>) {
  const space = (props.space as string) ?? 'widget-stage'
  // 宿主受控状态（G4 桥的三个演示目标）
  const [mode, setMode] = useState('medium')
  const [sortableItems, setSortableItems] = useState<string[]>(['alpha', 'beta', 'gamma'])
  const [inlineValue, setInlineValue] = useState('点击编辑我')

  const controlledBridge = useControlledSlotBridge('demo_controlled', {
    field: 'strength',
    get: () => mode,
    set: (_f, v) => setMode(v as string),
  })

  // 多声明分派：受控桥 + sortable/inline 的受控注入（同一 overrideProps 机制）
  const overrideProps = (declaration: WidgetDeclaration) => {
    const bridged = controlledBridge(declaration)
    if (bridged) return bridged
    if (declaration.id === 'demo_sortable') {
      return {
        items: sortableItems,
        onChange: (items: Array<{ label: string; value: string }>) =>
          setSortableItems(items.map((i) => String(i.value ?? i.label))),
      }
    }
    if (declaration.id === 'demo_inline') {
      return { value: inlineValue, onChange: (v: string) => setInlineValue(v) }
    }
    return undefined
  }

  return (
    <div className="space-y-4 p-3" data-testid="widget-stage">
      <DeclaredWidgetLayer space={space} overrideProps={overrideProps} />
    </div>
  )
}
