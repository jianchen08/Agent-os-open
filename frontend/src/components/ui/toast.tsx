import { Toaster as Sonner, toast } from 'sonner'

type ToasterProps = React.ComponentProps<typeof Sonner>

/**
 * Toast 通知容器组件
 * 基于 sonner 库实现，支持多种通知类型
 *
 * 使用方式:
 * 1. 在 App 根组件中添加 <Toaster />
 * 2. 使用 toast() 函数触发通知
 *
 * @example
 * toast.success('操作成功')
 * toast.error('操作失败')
 * toast.info('提示信息')
 * toast.warning('警告信息')
 */
function Toaster({ ...props }: ToasterProps) {
  return (
    <Sonner
      className="toaster group"
      toastOptions={{
        classNames: {
          toast:
            'group toast group-[.toaster]:bg-background group-[.toaster]:text-foreground group-[.toaster]:border-border group-[.toaster]:shadow-lg group-[.toaster]:rounded-xl',
          description: 'group-[.toast]:text-muted-foreground',
          actionButton: 'group-[.toast]:bg-primary group-[.toast]:text-primary-foreground',
          cancelButton: 'group-[.toast]:bg-muted group-[.toast]:text-muted-foreground',
          success:
            'group-[.toaster]:bg-status-success/10 group-[.toaster]:text-status-success group-[.toaster]:border-status-success/20',
          error:
            'group-[.toaster]:bg-status-error/10 group-[.toaster]:text-status-error group-[.toaster]:border-status-error/20',
          warning:
            'group-[.toaster]:bg-status-warning/10 group-[.toaster]:text-status-warning group-[.toaster]:border-status-warning/20',
          info: 'group-[.toaster]:bg-status-info/10 group-[.toaster]:text-status-info group-[.toaster]:border-status-info/20',
        },
      }}
      {...props}
    />
  )
}

export { Toaster, toast }
