import * as React from 'react'
import { cn } from '@/lib/utils'

/**
 * 输入框组件
 *
 * 样式由主题配置控制：
 * - 背景：使用 --bg-input 变量
 * - 聚焦边框：使用 --input-focus-border 变量
 * - 聚焦发光：使用 --input-focus-ring 变量
 * - 圆角：使用 --input-radius 变量
 * - 样式类型：使用 --input-style 变量（filled/outlined/underline）
 */
const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, type, ...props }, ref) => {
    return (
      <input
        type={type}
        className={cn(
          'flex w-full px-3 py-2 text-base shadow-sm transition-all duration-200',
          'box-border',
          'file:text-foreground file:border-0 file:bg-transparent file:text-sm file:font-medium',
          'placeholder:text-muted-foreground',
          'disabled:cursor-not-allowed disabled:opacity-50',
          'md:text-sm',
          'focus:outline-none',
          className,
        )}
        style={{
          backgroundColor: 'var(--bg-input, hsl(var(--background)))',
          border: '1px solid hsl(var(--border))',
          borderRadius: 'var(--input-radius, 0.5rem)',
        }}
        ref={ref}
        {...props}
      />
    )
  },
)
Input.displayName = 'Input'

export { Input }
