/**
 * 表单交互组件（渲染/校验引擎已统一到 RjsfForm）
 *
 * 聊天/工作区空间的动态表单 widget。字段词汇表（input/textarea/select/toggle/
 * number/slider/color/date/multiselect/radio/checkbox）已并入 UIInputFormField
 * 统一类型，本组件只做 props 收窄与提交回调透传。
 *
 * @module FormWidget
 */

import { RjsfForm } from '@/services/schema/RjsfForm'
import type { UIInputFormField } from '@/types/schema'

/**
 * 提取安全的字段数组
 *
 * @param fields - 原始字段定义
 * @returns 类型安全的 UIInputFormField 数组
 */
function extractFields(fields: unknown): UIInputFormField[] {
  if (!Array.isArray(fields)) return []
  return fields.filter(
    (f): f is UIInputFormField =>
      typeof f === 'object' && f !== null && typeof (f as UIInputFormField).name === 'string',
  )
}

/**
 * 表单交互组件
 *
 * @param props - 组件属性，包含 fields、layout、onSubmit 等
 * @returns 动态表单渲染结果
 */
export function FormWidget(props: Record<string, unknown>) {
  const fields = extractFields(props.fields)
  const onSubmit = props.onSubmit as ((data: Record<string, unknown>) => void) | undefined
  const layout = props.layout === 'grid' ? 'double' : 'single'

  return (
    <RjsfForm
      fields={fields}
      layout={layout}
      title={props.title as string | undefined}
      submitLabel={(props.submitLabel as string) ?? '提交'}
      onSubmit={onSubmit ? (values) => onSubmit(values) : undefined}
    />
  )
}
