/**
 * 作曲器模型信息单入口（P1-1 移动端视觉优化，MuMu 实测 shots/04 三处冗余）：
 *
 * 现状三处冗余：权限档「默认」chip 旁的「🧠」思考强度 chip + 底部整行模型名
 * 选择器——同一「模型/推理」语义占两行。本组件把它合并为单一入口：
 * 触发器 = 一颗图标 chip（Sparkles + 下拉箭头），点开在作曲器工具栏内展开
 * 内嵌面板（非浮窗），面板里并排「模型」选择器与「思考强度」选择器两个
 * 槽位渲染层（DeclaredWidgetLayer 原样搬运，声明契约/插件覆盖能力不变）。
 *
 * 形态裁定（记录被否方案）：
 * - radix Popover/DropdownMenu 承载：antd Select 下拉经 body portal，点击
 *   选项会被 Popover 判为 outside 而提前关闭（antd 不在 radix 层栈内），
 *   嵌套交互脆弱——否；
 * - 触发器常显当前模型短名：当前模型真值在 ModelSelectWidget 内部求值
 *   （显式 > 管道 state 停留 > fallback 三级），触发器无法免重复该逻辑，
 *   且「能省略的一律省略」（优化原则 3）——模型/强度现值展开即见——否；
 * - 底部模型行原样保留：占整行高度，作曲器总高收窄目标直接落空——否。
 */

import { useState, type ReactNode } from 'react'
import { ChevronDown, Sparkles } from '@/assets/icons'
import { useAndroidBackCloser } from '@/hooks/useAndroidBackCloser'
import { cn } from '@/lib/utils'

export interface ComposerModelEntryProps {
  /** 模型选择槽位渲染层（DeclaredWidgetLayer slotId=model_id 原样搬运） */
  modelSlot: ReactNode
  /** 思考强度槽位渲染层（DeclaredWidgetLayer slotId=thinking_strength；可空） */
  thinkingSlot?: ReactNode
  className?: string
}

export function ComposerModelEntry({ modelSlot, thinkingSlot, className }: ComposerModelEntryProps) {
  const [open, setOpen] = useState(false)
  // Android back 优先关面板（P1-8 装机复验缺口续修，MuMu 实测 v_05/v_06：面板
  // 开时按 back 面板不关、应用直接退桌面——本面板是自定义 div 非 radix/antd
  // 浮层，不落在 androidBackBridge 兜底腿探测面内，必须显式注册 LIFO）。开态
  // 注册 back 关面板并消费该按；关态自动注销，不吞后续 back（壳走既有语义）。
  useAndroidBackCloser(open, () => setOpen(false))

  return (
    <>
      <button
        type="button"
        data-testid="composer-model-entry"
        className={cn(
          'flex h-8 min-w-0 shrink-0 items-center gap-1 rounded-lg px-2 text-xs font-medium transition-all duration-200',
          'bg-primary text-primary-foreground hover:bg-primary/90 hover:shadow-sm',
          className,
        )}
        aria-expanded={open}
        title="模型与思考强度"
        aria-label="模型与思考强度"
        onClick={() => setOpen((v) => !v)}
      >
        <Sparkles className="h-icon-md w-icon-md shrink-0" />
        <ChevronDown
          className={cn('h-icon-xs w-icon-xs shrink-0 opacity-70 transition-transform', open && 'rotate-180')}
        />
      </button>
      {/* 内嵌面板：basis-full 独占工具栏一行（flex-wrap 环境内自然换行整行，
          不浮窗不 portal，槽位层在普通 DOM 内渲染——antd/radix 弹层零嵌套面） */}
      {open && (
        <div
          data-testid="composer-model-entry-panel"
          className="flex min-w-0 basis-full flex-col gap-1.5 rounded-lg border border-border/40 p-2"
        >
          <div className="flex min-w-0 items-center gap-2">
            <span className="text-muted-foreground shrink-0 text-[11px]">模型</span>
            <div className="flex min-w-0 flex-1 justify-start">{modelSlot}</div>
          </div>
          {thinkingSlot && (
            <div className="flex min-w-0 items-center gap-2">
              <span className="text-muted-foreground shrink-0 text-[11px]">思考强度</span>
              <div className="flex min-w-0 flex-1 justify-start">{thinkingSlot}</div>
            </div>
          )}
        </div>
      )}
    </>
  )
}
