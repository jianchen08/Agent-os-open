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
 * 审批通知选项
 */
export interface ApprovalToastOptions {
  /** 审批 ID */
  approvalId: string
  /** 会话 ID */
  sessionId: string
  /** 标题 */
  title: string
  /** 描述 */
  description?: string
  /** 批准回调 */
  onApprove?: () => void
  /** 拒绝回调 */
  onReject?: () => void
  /** 查看详情回调 */
  onViewDetails?: () => void
}

/**
 * 显示审批请求 Toast
 *
 * 带有"批准"、"拒绝"、"查看详情"三个操作按钮
 */
export function showApprovalToast(options: ApprovalToastOptions) {
  const { approvalId, title, description, onReject, onViewDetails } = options

  return sonnerToast.info(title, {
    id: `approval-${approvalId}`,
    description,
    duration: 60000, // 60秒，给用户足够时间决策
    action: {
      label: '查看详情',
      onClick: () => {
        onViewDetails?.()
      },
    },
    cancel: {
      label: '拒绝',
      onClick: () => {
        onReject?.()
      },
    },
    onDismiss: () => {
      // 用户关闭通知时的处理
    },
    onAutoClose: () => {
      // 自动关闭时的处理
    },
  })
}

/**
 * 显示带操作按钮的交互式 Toast
 */
export function showInteractiveToast(
  title: string,
  options: {
    description?: string
    duration?: number
    primaryAction?: {
      label: string
      onClick: () => void
    }
    secondaryAction?: {
      label: string
      onClick: () => void
    }
  },
) {
  return sonnerToast.info(title, {
    description: options.description,
    duration: options.duration || 30000,
    action: options.primaryAction,
    cancel: options.secondaryAction,
  })
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
