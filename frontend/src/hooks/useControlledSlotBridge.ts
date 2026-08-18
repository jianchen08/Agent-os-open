/**
 * 受控双向绑定桥（widget 化 G4）：宿主向声明组件注入受控 value/onChange。
 *
 * 背景：overrideProps 机制本身在 DeclaredWidgetLayer 上已通用，但受控注入
 * 目前只在 chat-input 槽位（思考强度样板）手写接线——本钩子把「宿主 → 声明
 * 组件」的受控桥标准化：宿主提供 get/set + 字段名（缺省从声明单字段表单
 * 自动取），返回可直接喂给 DeclaredWidgetLayer.overrideProps 的函数。
 *
 * 声明合同：目标组件应为单字段 form（fields[0].name 即受控字段）或宿主
 * 显式给 field。value={[field]: get(field)}，onChange=set(field, v[field])。
 */
import { useCallback } from 'react'
import type { WidgetDeclaration } from '@/services/schema/ContributionRegistry'

export interface ControlledSlotBridgeSpec {
  /** 受控字段名（缺省：声明为单字段表单时自动取 fields[0].name） */
  field?: string
  /** 读当前值 */
  get: (field: string) => unknown
  /** 写值 */
  set: (field: string, value: unknown) => void
  /**
   * 附加透传（如 disabled/placeholder）。
   * 传函数时惰性求值——在 overrideProps 被调用（渲染期）才执行，规避宿主
   * 组件里依赖后声明变量（TDZ）的问题；函数体保持与组件渲染同步。
   */
  extra?: Record<string, unknown> | (() => Record<string, unknown>)
}

/**
 * 目标受控字段名：显式 field 优先；否则取声明单字段表单的第一个字段名；
 * 多字段/无 fields 无法判定返回 null（该声明不做注入）。
 */
export function controlledFieldOf(
  declaration: WidgetDeclaration,
  explicit?: string,
): string | null {
  if (explicit) return explicit
  const fields = (declaration.props as { fields?: Array<{ name?: unknown }> } | undefined)?.fields
  if (!Array.isArray(fields) || fields.length !== 1) return null
  const first = fields[0]
  return first && typeof first.name === 'string' ? first.name : null
}

/**
 * 构造 overrideProps 注入器（受控桥）。
 *
 * @example
 * const overrideProps = useControlledSlotBridge('thinking_strength', {
 *   field: 'strength',
 *   get: () => currentStrength,
 *   set: (_f, v) => setCurrentStrength(v as ThinkingStrength),
 *   extra: () => ({ disabled }),
 * })
 * <DeclaredWidgetLayer space="chat-input" slotId="thinking_strength" overrideProps={overrideProps} />
 */
export function useControlledSlotBridge(
  slotId: string,
  spec: ControlledSlotBridgeSpec,
): (declaration: WidgetDeclaration) => Record<string, unknown> | undefined {
  return useCallback(
    (declaration) => {
      if (declaration.id !== slotId) return undefined
      const field = controlledFieldOf(declaration, spec.field)
      if (!field) return undefined
      const extra =
        typeof spec.extra === 'function' ? spec.extra() : spec.extra
      return {
        value: { [field]: spec.get(field) },
        onChange: (values: Record<string, unknown>) => spec.set(field, values?.[field]),
        ...(extra ?? {}),
      }
    },
    [slotId, spec],
  )
}
