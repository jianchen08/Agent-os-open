/**
 * 自定义确认弹窗 Hook
 *
 * 替代原生 confirm() 弹窗，提供可定制的异步确认对话框
 *
 * @module utils/confirm
 */

import { useState } from 'react'

export interface ConfirmDialogState {
  open: boolean
  message: string
  onConfirm: () => void
  onCancel: () => void
}

/**
 * 自定义确认弹窗 Hook
 *
 * 用法：
 * 1. 在组件中调用 const { confirm, dialogState, setDialogState } = useConfirmDialog()
 * 2. 用 await confirm('消息') 替代 confirm('消息')
 * 3. 在 JSX 中渲染 ConfirmDialog UI（根据 dialogState）
 */
export function useConfirmDialog() {
  const [dialogState, setDialogState] = useState<ConfirmDialogState>({
    open: false,
    message: '',
    onConfirm: () => {},
    onCancel: () => {},
  })

  const confirm = (message: string): Promise<boolean> => {
    return new Promise((resolve) => {
      setDialogState({
        open: true,
        message,
        onConfirm: () => {
          setDialogState((s) => ({ ...s, open: false }))
          resolve(true)
        },
        onCancel: () => {
          setDialogState((s) => ({ ...s, open: false }))
          resolve(false)
        },
      })
    })
  }

  return { confirm, dialogState, setDialogState }
}
