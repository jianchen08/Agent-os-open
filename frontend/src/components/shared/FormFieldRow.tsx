/**
 * FormFieldRow 和 FormSection 表单辅助组件
 *
 * 从 ApiSettingsPage 的 FieldRow/Section 提取为共享组件。
 * 提供统一的表单布局：水平排列的 label + 输入控件，以及带标题的配置区块。
 */

import type { ReactNode } from 'react'

/** FormFieldRow 组件属性 */
interface FormFieldRowProps {
  /** 标签文字 */
  label: string
  /** 关联的表单元素 id */
  htmlFor: string
  /** 输入控件 */
  children: ReactNode
}

/** FormSection 组件属性 */
interface FormSectionProps {
  /** 区块标题 */
  title: string
  /** 区块内容 */
  children: ReactNode
}

/**
 * 表单字段行
 *
 * label 与输入控件水平排列，label 固定 120px 右对齐。
 * 遵循 ApiSettingsPage 中 FieldRow 的布局模式。
 */
export function FormFieldRow({ label, htmlFor, children }: FormFieldRowProps) {
  return (
    <div className="flex items-start gap-4">
      <label
        htmlFor={htmlFor}
        className="text-muted-foreground min-w-[120px] shrink-0 pt-2 text-right text-sm"
      >
        {label}
      </label>
      <div className="flex-1">{children}</div>
    </div>
  )
}

/**
 * 表单配置区块
 *
 * 带标题的分组容器，用于将相关表单字段组织在一起。
 * 遵循 ApiSettingsPage 中 Section 的布局模式。
 */
export function FormSection({ title, children }: FormSectionProps) {
  return (
    <section className="mb-6">
      <h2 className="text-foreground mb-3 text-sm font-semibold">{title}</h2>
      <div className="space-y-3">{children}</div>
    </section>
  )
}
