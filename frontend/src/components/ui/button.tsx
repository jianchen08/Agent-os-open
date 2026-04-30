import { cva, type VariantProps } from 'class-variance-authority'
import * as React from 'react'
import { cn } from '@/lib/utils'

/**
 * 按钮组件变体配置
 *
 * 样式由主题配置控制：
 * - 圆角：由 --btn-radius 控制（基于 button.style）
 * - 颜色：由 --btn-*-bg/text/border 变量控制
 * - 阴影：由 --btn-shadow 控制
 * - 悬停效果：由 --btn-hover-effect 控制
 *
 * 尺寸系统（不由主题控制）：
 * - xs: 24px (h-6) - 超小按钮
 * - sm: 32px (h-8) - 小按钮
 * - md: 36px (h-9) - 中等按钮 (默认)
 * - lg: 40px (h-10) - 大按钮
 *
 * 图标按钮尺寸：
 * - icon-xs: 24px x 24px
 * - icon-sm: 32px x 32px
 * - icon-md: 36px x 36px
 * - icon-lg: 40px x 40px
 */
const buttonVariants = cva(
  'inline-flex items-center justify-center whitespace-nowrap text-sm font-medium transition-all duration-200 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 [&_svg]:pointer-events-none [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        default:
          'bg-[var(--btn-primary-bg)] text-[var(--btn-primary-text)] border border-[var(--btn-primary-border)] shadow-[var(--btn-shadow,0_4px_6px_-1px_rgba(0,0,0,0.1))] hover:bg-[var(--btn-primary-hover-bg)] hover:shadow-[var(--btn-shadow-hover,0_6px_10px_-1px_rgba(0,0,0,0.15))] active:scale-[0.98]',
        destructive:
          'bg-[var(--btn-destructive-bg)] text-[var(--btn-destructive-text)] border border-[var(--btn-destructive-border)] shadow-[var(--btn-shadow,0_4px_6px_-1px_rgba(0,0,0,0.1))] hover:bg-[var(--btn-destructive-hover-bg)] hover:shadow-[var(--btn-shadow-hover,0_6px_10px_-1px_rgba(0,0,0,0.15))] active:scale-[0.98]',
        outline:
          'border border-[var(--btn-secondary-border,rgba(255,255,255,0.2))] bg-[var(--btn-secondary-bg,transparent)] text-[var(--btn-secondary-text)] hover:bg-[var(--btn-secondary-hover-bg,rgba(255,255,255,0.1))] hover:border-[var(--btn-secondary-border,rgba(255,255,255,0.3))] active:scale-[0.98]',
        secondary:
          'bg-[var(--btn-secondary-bg)] text-[var(--btn-secondary-text)] border border-[var(--btn-secondary-border)] shadow-[var(--btn-shadow,0_2px_4px_-1px_rgba(0,0,0,0.05))] hover:bg-[var(--btn-secondary-hover-bg)] hover:shadow-[var(--btn-shadow-hover,0_4px_6px_-1px_rgba(0,0,0,0.1))] active:scale-[0.98]',
        ghost:
          'bg-[var(--btn-ghost-bg,transparent)] text-[var(--btn-ghost-text)] border border-transparent hover:bg-[var(--btn-ghost-hover-bg,rgba(255,255,255,0.05))] active:scale-[0.98]',
        link: 'text-[var(--btn-primary-bg)] underline-offset-4 hover:underline',
      },
      size: {
        xs: 'h-6 px-2 gap-1.5 text-xs rounded-[var(--btn-radius,0.5rem)] [&_svg]:size-3.5',
        sm: 'h-8 px-3 gap-2 text-[13px] rounded-[var(--btn-radius,0.5rem)] [&_svg]:size-4',
        md: 'h-9 px-4 gap-2 text-sm rounded-[var(--btn-radius,0.5rem)] [&_svg]:size-[18px]',
        lg: 'h-10 px-5 gap-2.5 text-[15px] rounded-[var(--btn-radius,0.5rem)] [&_svg]:size-5',
        'icon-xs': 'h-6 w-6 rounded-[var(--btn-radius,0.5rem)] [&_svg]:size-3.5',
        'icon-sm': 'h-8 w-8 rounded-[var(--btn-radius,0.5rem)] [&_svg]:size-4',
        'icon-md': 'h-9 w-9 rounded-[var(--btn-radius,0.5rem)] [&_svg]:size-[18px]',
        'icon-lg': 'h-10 w-10 rounded-[var(--btn-radius,0.5rem)] [&_svg]:size-5',
        default: 'h-9 px-4 gap-2 text-sm rounded-[var(--btn-radius,0.5rem)] [&_svg]:size-[18px]',
        icon: 'h-9 w-9 rounded-[var(--btn-radius,0.5rem)] [&_svg]:size-[18px]',
      },
    },
    defaultVariants: {
      variant: 'default',
      size: 'md',
    },
  },
)

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>, VariantProps<typeof buttonVariants> {}

const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, ...props }, ref) => {
    return (
      <button className={cn(buttonVariants({ variant, size, className }))} ref={ref} {...props} />
    )
  },
)
Button.displayName = 'Button'

export { Button, buttonVariants }
