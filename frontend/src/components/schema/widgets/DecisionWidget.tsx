/**
 * 决策选择组件
 *
 * 根据 Schema 渲染决策选项，支持单选/多选模式。
 * 渲染为按钮组或选项卡片，选中后调用 onDecision 回调。
 *
 * @module DecisionWidget
 */

import React, { useState, useCallback } from 'react'

/** 选项样式 */
type OptionStyle = 'primary' | 'danger' | 'default'

/** 选项定义 */
interface DecisionOption {
  /** 选项唯一标识 */
  id: string
  /** 选项文本 */
  label: string
  /** 选项描述 */
  description?: string
  /** 选项样式 */
  style?: OptionStyle
  /** 是否禁用 */
  disabled?: boolean
  /** 图标 */
  icon?: string
}

/**
 * 提取选项数组
 *
 * @param options - 原始选项数据
 * @returns 类型安全的 DecisionOption 数组
 */
function extractOptions(options: unknown): DecisionOption[] {
  if (!Array.isArray(options)) return []
  return options.filter(
    (o): o is DecisionOption =>
      typeof o === 'object' && o !== null && typeof (o as DecisionOption).id === 'string',
  )
}

/** 选项样式映射 */
const OPTION_STYLE_MAP: Record<
  OptionStyle,
  { base: string; selected: string; hover: string }
> = {
  primary: {
    base: 'border-status-info/20 text-status-info hover:border-status-info/40',
    selected: 'bg-status-info border-status-info text-white',
    hover: 'hover:bg-status-info/10',
  },
  danger: {
    base: 'border-status-error/20 text-status-error hover:border-status-error/40',
    selected: 'bg-status-error border-status-error text-white',
    hover: 'hover:bg-status-error/10',
  },
  default: {
    base: 'border-border text-foreground hover:border-foreground/30',
    selected: 'bg-primary border-primary text-primary-foreground',
    hover: 'hover:bg-muted/50',
  },
}

/**
 * 决策选择组件
 *
 * 支持单选和多选模式，选中后调用 onDecision 回调。
 *
 * @param props - 组件属性，包含 options、multiple、onDecision 等
 * @returns 决策选项渲染结果
 */
export function DecisionWidget(props: Record<string, unknown>) {
  const options = extractOptions(props.options)
  const multiple = (props.multiple as boolean) ?? false
  const onDecision = props.onDecision as
    | ((selected: string | string[]) => void)
    | undefined
  const title = props.title as string | undefined
  const layout = (props.layout as 'buttons' | 'cards') ?? 'cards'

  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())

  const handleSelect = useCallback(
    (optionId: string) => {
      let newSelected: Set<string>

      if (multiple) {
        newSelected = new Set(selectedIds)
        if (newSelected.has(optionId)) {
          newSelected.delete(optionId)
        } else {
          newSelected.add(optionId)
        }
      } else {
        newSelected = new Set(selectedIds.has(optionId) ? [] : [optionId])
      }

      setSelectedIds(newSelected)

      if (onDecision) {
        const selected = Array.from(newSelected)
        onDecision(multiple ? selected : (selected[0] ?? ''))
      }
    },
    [selectedIds, multiple, onDecision],
  )

  if (options.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center rounded-lg border border-dashed p-8">
        <svg
          className="text-muted-foreground mb-2 h-12 w-12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.5}
        >
          <path d="M9 5l7 7-7 7" />
        </svg>
        <p className="text-muted-foreground text-sm">暂无可选项</p>
      </div>
    )
  }

  return (
    <div className="w-full space-y-3">
      {title && (
        <h3 className="text-foreground text-sm font-semibold">{title}</h3>
      )}

      {layout === 'buttons' ? (
        <div className="flex flex-wrap gap-2">
          {options.map((option) => {
            const isSelected = selectedIds.has(option.id)
            const styleConfig = OPTION_STYLE_MAP[option.style ?? 'default']

            return (
              <button
                key={option.id}
                onClick={() => handleSelect(option.id)}
                disabled={option.disabled}
                className={`rounded-lg border px-4 py-2 text-sm font-medium transition-all ${
                  isSelected
                    ? styleConfig.selected
                    : `${styleConfig.base} ${styleConfig.hover}`
                } ${option.disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer'}`}
              >
                {option.icon && <span className="mr-1.5">{option.icon}</span>}
                {option.label}
              </button>
            )
          })}
        </div>
      ) : (
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
          {options.map((option) => {
            const isSelected = selectedIds.has(option.id)
            const styleConfig = OPTION_STYLE_MAP[option.style ?? 'default']

            return (
              <div
                key={option.id}
                onClick={option.disabled ? undefined : () => handleSelect(option.id)}
                className={`cursor-pointer rounded-lg border p-3 transition-all ${
                  isSelected
                    ? `${styleConfig.selected} ring-2 ring-offset-1`
                    : `${styleConfig.base} ${styleConfig.hover}`
                } ${option.disabled ? 'cursor-not-allowed opacity-50' : ''}`}
              >
                <div className="flex items-center gap-2">
                  {/* 选中指示器 */}
                  <div
                    className={`flex h-5 w-5 shrink-0 items-center justify-center rounded-full border-2 ${
                      isSelected
                        ? 'border-current bg-current'
                        : 'border-muted-foreground/30'
                    }`}
                  >
                    {isSelected && (
                      <svg
                        className="h-3 w-3 text-white"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth={3}
                      >
                        <path d="M5 13l4 4L19 7" />
                      </svg>
                    )}
                  </div>

                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-1.5">
                      {option.icon && <span>{option.icon}</span>}
                      <span className="text-sm font-medium">{option.label}</span>
                    </div>
                    {option.description && (
                      <p className="mt-0.5 text-xs opacity-70">{option.description}</p>
                    )}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* 已选状态提示 */}
      {selectedIds.size > 0 && (
        <div className="text-muted-foreground text-xs">
          已选择 {selectedIds.size} 项
        </div>
      )}
    </div>
  )
}
