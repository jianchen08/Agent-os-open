/**
 * PageRenderer — 统一页面渲染器（阶段2 前端部分B）
 *
 * 消费 PageDeclaration[]（contributionRegistry.pages 唯一真相源），按 space/slot 分发：
 * - widget 字段（L3 整体自定义组件）→ widgetRegistry.get / findFallback 拿组件渲染（props 透传）
 * - schema 字段（L1 字段级配置）→ schemaToFields 适配为 UIInputFormField[] 后交给 SchemaDriver 渲染表单
 * - dock 空间 → 状态条目行
 * - 其余空间 / 空内容 → 占位（保持页面可见，不崩溃）
 *
 * 关联：统一能力架构第四章 — 渲染层按 space 分发（PageRenderer 即分发器）。
 */

import { useMemo } from 'react'
import { DeclaredWidgetLayer } from '@/components/schema/DeclaredWidgetLayer'
import { cn } from '@/lib/utils'
import apiClient from '@/services/api/client'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { SchemaDriver } from '@/services/schema/SchemaDriver'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { windowManager } from '@/services/window/WindowManager'
import type { PageDeclaration, PageSlot, PageSpace } from '@/services/schema/ContributionRegistry'
import type { UIInputFormField } from '@/types/schema'
import type { ReactNode } from 'react'

/** SchemaDriver 支持的字段类型集合（统一词汇表：未知类型回退 string） */
const FORM_FIELD_TYPES = new Set([
  'string', 'number', 'boolean', 'select', 'multiselect', 'textarea', 'date', 'file',
  'input', 'toggle', 'slider', 'color', 'radio', 'checkbox',
])

/**
 * 将 PageDeclaration.schema 适配为 UIInputFormField[]
 *
 * 支持两种形态：
 * 1. `{ fields: UIInputFormField[] }`（统一模型推荐形态）——逐项收窄类型，未知类型回退 string
 * 2. JSON Schema `{ type: 'object', properties: {...} }` ——properties 转字段（enum → select）
 */
export function schemaToFields(schema: Record<string, unknown>): UIInputFormField[] {
  if (!schema || typeof schema !== 'object') return []

  const rawFields = schema.fields
  if (Array.isArray(rawFields)) {
    const out: UIInputFormField[] = []
    for (const raw of rawFields) {
      const field = normalizeFormField(raw as Record<string, unknown>)
      if (field) out.push(field)
    }
    return out
  }

  if (schema.type === 'object' && schema.properties && typeof schema.properties === 'object') {
    const out: UIInputFormField[] = []
    for (const [name, def] of Object.entries(schema.properties as Record<string, unknown>)) {
      const field = jsonSchemaToField(name, def as Record<string, unknown>)
      if (field) out.push(field)
    }
    return out
  }

  return []
}

/** 收窄单个字段为 UIInputFormField（缺 name 丢弃；未知 type 回退 string） */
function normalizeFormField(raw: Record<string, unknown>): UIInputFormField | null {
  const name = typeof raw.name === 'string' ? raw.name : ''
  if (!name) return null
  const type = FORM_FIELD_TYPES.has(String(raw.type)) ? (String(raw.type) as UIInputFormField['type']) : 'string'
  return {
    name,
    type,
    label: typeof raw.label === 'string' ? raw.label : name,
    description: typeof raw.description === 'string' ? raw.description : undefined,
    default: raw.default,
    required: Boolean(raw.required),
    options: Array.isArray(raw.options) ? (raw.options as UIInputFormField['options']) : undefined,
    datasourceUri: typeof raw.datasourceUri === 'string' ? raw.datasourceUri : undefined,
    placeholder: typeof raw.placeholder === 'string' ? raw.placeholder : undefined,
    validation: raw.validation as UIInputFormField['validation'] | undefined,
  }
}

/** JSON Schema property → UIInputFormField（enum → select；integer → number） */
function jsonSchemaToField(name: string, def: Record<string, unknown>): UIInputFormField | null {
  if (!def || typeof def !== 'object') return null
  const type = def.type === 'integer' ? 'number' : String(def.type ?? 'string')
  const label = typeof def.title === 'string' ? def.title : name
  const enumValues = Array.isArray(def.enum) ? def.enum : undefined
  if (enumValues && enumValues.length > 0) {
    return {
      name,
      type: 'select',
      label,
      description: typeof def.description === 'string' ? def.description : undefined,
      required: Boolean(def.required),
      default: def.default,
      options: enumValues.map((v) => ({ label: String(v), value: v as string | number })),
    }
  }
  const fieldType = FORM_FIELD_TYPES.has(type) ? (type as UIInputFormField['type']) : 'string'
  return {
    name,
    type: fieldType,
    label,
    description: typeof def.description === 'string' ? def.description : undefined,
    required: Boolean(def.required),
    default: def.default,
  }
}

/**
 * 按 page 声明生成表单提交处理器：
 * - 声明 datasourceUri → PUT 到数据源（配置类页面写回）
 * - 无数据源 → 本地 no-op（表单仅交互，不持久化）
 */
function buildSubmitHandler(
  page: PageDeclaration,
): (values: Record<string, unknown>) => void | Promise<void> {
  if (typeof page.datasourceUri === 'string' && page.datasourceUri) {
    return async (values) => {
      await apiClient.put(page.datasourceUri as string, values)
    }
  }
  return async () => {
    /* 无数据源：仅本地交互 */
  }
}

/** 占位渲染（widget 未注册 / 页面无内容 / 未支持空间） */
function PagePlaceholder({ page, reason }: { page: PageDeclaration; reason?: string }) {
  return (
    <div
      data-testid={`page-placeholder-${page.id}`}
      className="text-muted-foreground flex h-full min-h-[60px] flex-col items-center justify-center gap-1 p-4 text-center text-xs"
    >
      <span>
        {page.icon ? `${page.icon} ` : ''}
        {page.title ?? page.id}
      </span>
      <span>{reason ?? '该页面暂无可渲染内容'}</span>
    </div>
  )
}

/**
 * 判断 page 是否声明了任何 detachable 弹出能力（popout/childWindow/desktopWidget）
 *
 * 用于决定是否在 page 渲染区显示「弹出」按钮。
 */
function isDetachable(page: PageDeclaration): boolean {
  const d = page.detachable
  if (!d) return false
  return Boolean(d.popout || d.childWindow || d.desktopWidget)
}

/** 弹出按钮（绝对定位到 page 右上角） */
function PagePopoutButton({ page }: { page: PageDeclaration }) {
  return (
    <button
      type="button"
      data-testid="page-popout-btn"
      title="弹出为浮窗"
      aria-label="弹出为浮窗"
      onClick={() => windowManager.openPopout(page)}
      className="bg-background/80 hover:bg-accent text-muted-foreground hover:text-foreground pointer-events-auto absolute right-1 top-1 z-10 flex h-6 w-6 items-center justify-center rounded text-xs opacity-0 transition-opacity group-hover:opacity-100 focus:opacity-100"
    >
      {/* 弹出/外链图标 */}
      <svg
        width="12"
        height="12"
        viewBox="0 0 12 12"
        fill="none"
        aria-hidden="true"
        className="shrink-0"
      >
        <path
          d="M5 2H2.5A1.5 1.5 0 0 0 1 3.5v6A1.5 1.5 0 0 0 2.5 11h6A1.5 1.5 0 0 0 10 9.5V7"
          stroke="currentColor"
          strokeWidth="1.2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
        <path
          d="M6.5 1.5h4v4M10.5 1.5 6 6"
          stroke="currentColor"
          strokeWidth="1.2"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
    </button>
  )
}

/**
 * 渲染单个页面内容（纯分发函数，PageRenderer 与外部组件共用）
 *
 * 分发优先级：
 * 1. widget（L3 整体自定义组件，widgetRegistry 未命中走 findFallback 降级）
 * 2. schema（L1 字段级配置 → SchemaDriver 表单）
 * 3. dock 空间 → 状态条目行
 * 4. 其余 → 占位
 */
export function renderPageContent(page: PageDeclaration): ReactNode {
  // 1) widget 优先（workspace/settings/chat 等空间的 L3 自定义组件）
  if (page.widget) {
    const Widget = widgetRegistry.get(page.widget) ?? widgetRegistry.findFallback(page.widget)
    if (Widget) {
      return <Widget {...(page.props ?? {})} />
    }
    return <PagePlaceholder page={page} reason={`组件 ${page.widget} 未注册`} />
  }

  // 2) schema → SchemaDriver 表单（配置类页面）
  const fields = page.schema ? schemaToFields(page.schema) : []
  if (fields.length > 0) {
    return (
      <SchemaDriver
        fields={fields}
        title={page.title}
        submitLabel="保存"
        onSubmit={buildSubmitHandler(page)}
      />
    )
  }

  // 3) dock 空间：状态条目行
  if (page.space === 'dock') {
    return (
      <div
        data-testid={`dock-page-${page.id}`}
        className="flex items-center gap-1.5 whitespace-nowrap"
      >
        <span className="inline-block h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--ds-status-pending,#94A3B8)]" />
        <span className="text-muted-foreground text-[11px] leading-none">{page.title ?? page.id}</span>
      </div>
    )
  }

  // 4) 其余空间 / 空内容 → 占位
  return <PagePlaceholder page={page} />
}

export interface PageRendererProps {
  /** 页面集合（缺省时从 contributionRegistry 取；与 space 同时给出则先按 space 过滤） */
  pages?: PageDeclaration[]
  /** 目标空间过滤 */
  space?: PageSpace
  /** 空间内栏位过滤 */
  slot?: PageSlot
  className?: string
}

/**
 * PageRenderer — 按 space/slot 分发渲染页面集合
 *
 * 数据来源：`pages` props 优先，否则 contributionRegistry（getPages /
 * getPagesBySpace）。渲染逻辑全部委托 renderPageContent。
 */
export function PageRenderer({ pages, space, slot, className }: PageRendererProps) {
  const source = pages ?? (space ? contributionRegistry.getPagesBySpace(space) : contributionRegistry.getPages())

  const resolved = useMemo(() => {
    let list = source
    if (space) list = list.filter((p) => p.space === space)
    if (slot) list = list.filter((p) => p.slot === slot)
    return [...list].sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
  }, [source, space, slot])

  if (resolved.length === 0) {
    return (
      <div className={cn('text-muted-foreground flex h-full items-center justify-center text-xs', className)}>
        暂无页面
      </div>
    )
  }

  return (
    <div className={cn('flex flex-col gap-1', className)} data-testid="page-renderer">
      {resolved.map((page) => (
        <div
          key={page.id}
          data-testid={`page-${page.id}`}
          className="group relative min-h-0"
        >
          {renderPageContent(page)}
          {isDetachable(page) ? <PagePopoutButton page={page} /> : null}
        </div>
      ))}
      {/* ui_schema 声明 widget 的渲染层（架构 §5.3 生产消费：闭合 getAllWidgets 零消费者断链）。
          无声明时返回 null，不改变既有 DOM。M1 起细化各空间放置。 */}
      {space ? <DeclaredWidgetLayer space={space} /> : null}
    </div>
  )
}

export default PageRenderer
