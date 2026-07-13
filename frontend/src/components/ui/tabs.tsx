import * as React from 'react'
import { cn } from '@/lib/utils'

/**
 * 标签页组件
 *
 * 样式由主题配置控制：
 * - 圆角：由 --tabs-radius 控制
 * - 列表背景：由 --tabs-list-bg 控制
 * - 激活态：由 --tabs-active-bg/text 控制
 * - 非激活态：由 --tabs-inactive-text 控制
 */

interface TabsContextValue {
  value: string
  onValueChange: (value: string) => void
}

const TabsContext = React.createContext<TabsContextValue | undefined>(undefined)

function useTabsContext() {
  const context = React.useContext(TabsContext)
  if (!context) {
    throw new Error('Tabs 组件必须在 Tabs 内部使用')
  }
  return context
}

interface TabsProps extends React.HTMLAttributes<HTMLDivElement> {
  value?: string
  defaultValue?: string
  onValueChange?: (value: string) => void
}

const Tabs = React.forwardRef<HTMLDivElement, TabsProps>(
  ({ className, value, defaultValue, onValueChange, children, ...props }, ref) => {
    const [internalValue, setInternalValue] = React.useState(defaultValue ?? '')

    const currentValue = value ?? internalValue
    const handleValueChange = React.useCallback(
      (newValue: string) => {
        if (value === undefined) {
          setInternalValue(newValue)
        }
        onValueChange?.(newValue)
      },
      [value, onValueChange],
    )

    return (
      <TabsContext.Provider value={{ value: currentValue, onValueChange: handleValueChange }}>
        <div ref={ref} className={cn('w-full', className)} {...props}>
          {children}
        </div>
      </TabsContext.Provider>
    )
  },
)
Tabs.displayName = 'Tabs'

const TabsList = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(
  ({ className, ...props }, ref) => (
    <div
      ref={ref}
      role="tablist"
      className={cn(
        'text-muted-foreground inline-flex h-9 items-center justify-center p-1',
        className,
      )}
      style={{
        borderRadius: 'var(--tabs-radius, 0.5rem)',
        backgroundColor: 'var(--tabs-list-bg, rgba(255, 255, 255, 0.05))',
      }}
      {...props}
    />
  ),
)
TabsList.displayName = 'TabsList'

interface TabsTriggerProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  value: string
}

const TabsTrigger = React.forwardRef<HTMLButtonElement, TabsTriggerProps>(
  ({ className, value, ...props }, ref) => {
    const { value: selectedValue, onValueChange } = useTabsContext()
    const isSelected = selectedValue === value

    return (
      <button
        ref={ref}
        type="button"
        role="tab"
        aria-selected={isSelected}
        data-state={isSelected ? 'active' : 'inactive'}
        className={cn(
          'ring-offset-background focus-visible:ring-ring inline-flex items-center justify-center px-3 py-1 text-sm font-medium whitespace-nowrap transition-all focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none disabled:pointer-events-none disabled:opacity-50',
          className,
        )}
        style={{
          borderRadius: 'var(--tabs-radius, 0.5rem)',
          backgroundColor: isSelected ? 'var(--tabs-active-bg, #1e293b)' : 'transparent',
          color: isSelected
            ? 'var(--tabs-active-text, #f8fafc)'
            : 'var(--tabs-inactive-text, #94a3b8)',
          boxShadow: isSelected ? 'var(--btn-shadow, 0 2px 4px rgba(0,0,0,0.1))' : 'none',
        }}
        onClick={() => onValueChange(value)}
        {...props}
      />
    )
  },
)
TabsTrigger.displayName = 'TabsTrigger'

interface TabsContentProps extends React.HTMLAttributes<HTMLDivElement> {
  value: string
}

const TabsContent = React.forwardRef<HTMLDivElement, TabsContentProps>(
  ({ className, value, ...props }, ref) => {
    const { value: selectedValue } = useTabsContext()
    const isSelected = selectedValue === value

    if (!isSelected) {
      return null
    }

    return (
      <div
        ref={ref}
        role="tabpanel"
        data-state={isSelected ? 'active' : 'inactive'}
        className={cn(
          'ring-offset-background focus-visible:ring-ring mt-2 focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:outline-none',
          className,
        )}
        {...props}
      />
    )
  },
)
TabsContent.displayName = 'TabsContent'

export { Tabs, TabsList, TabsTrigger, TabsContent }
