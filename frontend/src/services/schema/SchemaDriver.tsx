/**
 * SchemaDriver 表单驱动组件（渲染/校验引擎已统一到 RjsfForm）
 *
 * 消费后端字段级 Schema（`UIInputFormField[]`，来源 GET /ext/agent_manager/agents/schema、
 * contributes.pages[].schema）自动生成表单。组件签名保持不变，内部委托
 * RjsfForm（react-jsonschema-form + antd 主题）。
 *
 * datasource 工具函数（normalizeOptions / fetchDatasourceOptions）迁至 RjsfForm，
 * 此处 re-export 维持既有导入路径。
 *
 * @module SchemaDriver
 */

import { RjsfForm } from '@/services/schema/RjsfForm'
import type { UIInputFormField } from '@/types/schema'

export { normalizeOptions, fetchDatasourceOptions, type SchemaOption } from '@/services/schema/RjsfForm'

/** SchemaDriver 组件属性 */
export interface SchemaDriverProps {
  /** 字段定义（UIInputFormField[]） */
  fields: UIInputFormField[]
  /** 初始值（编辑场景：从 yaml/JSON 解析出的对象） */
  initialValues?: Record<string, unknown>
  /** 提交回调（可返回 Promise，提交期间按钮显示 loading） */
  onSubmit: (values: Record<string, unknown>) => void | Promise<void>
  /** 提交按钮文案 */
  submitLabel?: string
  /** 表单标题（可选） */
  title?: string
  /** 布局：single 单列 / double 双列 */
  layout?: 'single' | 'double'
}

/**
 * SchemaDriver 表单驱动组件
 *
 * @param props - 字段定义、初始值、提交回调等
 * @returns 动态表单
 */
export function SchemaDriver(props: SchemaDriverProps) {
  return <RjsfForm {...props} />
}

export default SchemaDriver
