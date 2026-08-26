/**
 * 错误来源标签（统一错误模型：config/error_codes.json sources.enum 单一真值源）。
 * 前端据此区分错误来自内核(Rust)/插件(Python)/LLM/基础设施/前端自身。
 */

import type { ErrorSource } from '../../types/api'

/** 来源 → 展示文案 */
export const ERROR_SOURCE_LABEL: Record<ErrorSource, string> = {
  kernel: '内核',
  plugin: '插件',
  llm: '模型',
  infra: '网络',
  frontend: '前端',
}

/** 来源 → 标签样式（语义色，与主题 token 对齐） */
const ERROR_SOURCE_STYLE: Record<ErrorSource, string> = {
  kernel: 'bg-status-error/10 text-status-error border-status-error/30',
  plugin: 'bg-status-warning/10 text-status-warning border-status-warning/30',
  llm: 'bg-status-info/10 text-status-info border-status-info/30',
  infra: 'bg-muted/30 text-muted-foreground border-border/40',
  frontend: 'bg-status-info/10 text-status-info border-status-info/30',
}

/** 未知来源兜底（旧后端/未带 source 的错误） */
const UNKNOWN_SOURCE_STYLE = 'bg-muted/30 text-muted-foreground border-border/40'

/**
 * 来源标签徽标。source 缺失时渲染「未知」灰标（旧后端兼容，不炸渲染）。
 */
export function ErrorSourceBadge({ source }: { source?: ErrorSource | string }) {
  const normalized = isErrorSource(source) ? source : undefined
  return (
    <span
      className={`inline-flex items-center rounded border px-1 py-px text-[10px] font-medium leading-none ${normalized ? ERROR_SOURCE_STYLE[normalized] : UNKNOWN_SOURCE_STYLE}`}
      data-testid="error-source-badge"
    >
      {normalized ? ERROR_SOURCE_LABEL[normalized] : '未知'}
    </span>
  )
}

function isErrorSource(value: unknown): value is ErrorSource {
  return (
    value === 'kernel' ||
    value === 'plugin' ||
    value === 'llm' ||
    value === 'infra' ||
    value === 'frontend'
  )
}
