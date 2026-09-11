/**
 * Toast 通知组件
 *
 * 使用 sonner 库实现 Toast 通知功能
 * 支持审批请求等交互式通知
 */

import { Toaster as SonnerToaster, toast as sonnerToast } from 'sonner'

/**
 * Toaster 组件
 *
 * 在应用根组件中使用，用于显示 toast 通知
 */
export function Toaster() {
  return (
    <SonnerToaster
      position="top-right"
      richColors
      closeButton
      duration={10000}
      toastOptions={{
        style: {
          background: 'hsl(var(--background))',
          color: 'hsl(var(--foreground))',
          border: '1px solid hsl(var(--border))',
        },
      }}
    />
  )
}

/**
 * 导出 toast 函数
 *
 * 使用示例:
 * import { toast } from './components/ui/sonner';
 * toast.success('操作成功');
 * toast.error('操作失败');
 * toast.info('提示信息');
 * toast.warning('警告信息');
 */
export const toast = {
  success: (message: string, options?: { description?: string }) => {
    return sonnerToast.success(message, options)
  },
  error: (message: string, options?: { description?: string }) => {
    return sonnerToast.error(message, options)
  },
  info: (message: string, options?: { description?: string; duration?: number }) => {
    return sonnerToast.info(message, options)
  },
  warning: (message: string, options?: { description?: string }) => {
    return sonnerToast.warning(message, options)
  },
  loading: (message: string, options?: { description?: string }) => {
    return sonnerToast.loading(message, options)
  },
  dismiss: (id?: string) => {
    sonnerToast.dismiss(id)
  },
  promise: <T,>(
    promise: Promise<T>,
    options: {
      loading: string
      success: string
      error: string
    },
  ) => {
    return sonnerToast.promise(promise, options)
  },
}
