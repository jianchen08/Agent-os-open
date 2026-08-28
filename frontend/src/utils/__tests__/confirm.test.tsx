// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * useConfirmDialog Hook 测试
 *
 * 覆盖：confirm() 打开对话框、确认/取消后关闭并 resolve 对应布尔值、
 * setDialogState 直接更新状态。
 */

import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { useConfirmDialog } from '@/utils/confirm'

describe('useConfirmDialog', () => {
  it('初始状态为关闭且消息为空', () => {
    const { result } = renderHook(() => useConfirmDialog())

    expect(result.current.dialogState.open).toBe(false)
    expect(result.current.dialogState.message).toBe('')
  })

  it('confirm() 打开对话框并记录消息', () => {
    const { result } = renderHook(() => useConfirmDialog())

    let promise: Promise<boolean> | undefined
    act(() => {
      promise = result.current.confirm('确定删除？')
    })

    expect(result.current.dialogState.open).toBe(true)
    expect(result.current.dialogState.message).toBe('确定删除？')
    expect(promise).toBeInstanceOf(Promise)
  })

  it('确认后关闭对话框并 resolve true', async () => {
    const { result } = renderHook(() => useConfirmDialog())

    let promise: Promise<boolean> | undefined
    act(() => {
      promise = result.current.confirm('确定？')
    })

    act(() => {
      result.current.dialogState.onConfirm()
    })

    expect(result.current.dialogState.open).toBe(false)
    await expect(promise).resolves.toBe(true)
  })

  it('取消后关闭对话框并 resolve false', async () => {
    const { result } = renderHook(() => useConfirmDialog())

    let promise: Promise<boolean> | undefined
    act(() => {
      promise = result.current.confirm('确定？')
    })

    act(() => {
      result.current.dialogState.onCancel()
    })

    expect(result.current.dialogState.open).toBe(false)
    await expect(promise).resolves.toBe(false)
  })

  it('setDialogState 可直接更新状态', () => {
    const { result } = renderHook(() => useConfirmDialog())

    act(() => {
      result.current.setDialogState((s) => ({ ...s, open: true, message: '手动打开' }))
    })

    expect(result.current.dialogState.open).toBe(true)
    expect(result.current.dialogState.message).toBe('手动打开')
  })
})
