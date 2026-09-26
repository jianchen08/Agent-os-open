/** @feature: FP-0.2.四 前端 Schema | @ci: frontend-test */
/**
 * sonner 包装层补测
 *
 * src/components/ui/sonner.tsx 是 sonner 库的样式包装（Toaster 配置 + toast
 * 六方法转发 + promise 转发）。本文件以真实 sonner 渲染驱动：
 * - Toaster：挂载渲染（offset/richColors/closeButton 为透传 props，不重复
 *   断言库内部行为）
 * - toast.success/error/info/warning/loading：消息与 description 进 DOM
 * - toast.dismiss：按 id 移除已显示 toast
 * - toast.promise：resolve 面走 success 文案；reject 面走 error 文案
 *
 * 不可达/未覆盖说明（本文件 docstring 存证）：无——包装层全分支可达。
 */
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Toaster, toast } from '../sonner'

describe('Toaster 挂载', () => {
  it('渲染且不崩', () => {
    const { container } = render(<Toaster />)
    expect(container.firstChild).not.toBeNull()
  })
})

describe('toast 五个静态方法', () => {
  it.each([
    ['success', '保存成功'],
    ['error', '保存失败'],
    ['info', '友情提示'],
    ['warning', '注意风险'],
    ['loading', '处理中'],
  ] as const)('toast.%s 显示消息与 description', async (method, message) => {
    render(<Toaster />)
    toast[method](message, { description: `${message}-desc` })
    expect(await screen.findByText(message)).toBeInTheDocument()
    expect(screen.getByText(`${message}-desc`)).toBeInTheDocument()
  })
})

describe('toast.dismiss', () => {
  it('按 id 移除已显示 toast，未传 id 时整体兜底不崩', async () => {
    render(<Toaster />)
    const id = toast.info('会消失的提示')
    expect(await screen.findByText('会消失的提示')).toBeInTheDocument()
    toast.dismiss(id)
    await waitFor(() => {
      expect(screen.queryByText('会消失的提示')).not.toBeInTheDocument()
    })
    expect(() => toast.dismiss()).not.toThrow()
  })
})

describe('toast.promise', () => {
  it('resolve 面显示 success 文案', async () => {
    render(<Toaster />)
    toast.promise(Promise.resolve('v'), {
      loading: '提交中',
      success: '提交完成',
      error: '提交失败',
    })
    expect(await screen.findByText('提交完成')).toBeInTheDocument()
  })

  it('reject 面显示 error 文案', async () => {
    render(<Toaster />)
    toast.promise(Promise.reject(new Error('boom')), {
      loading: '提交中',
      success: '提交完成',
      error: '提交失败',
    })
    expect(await screen.findByText('提交失败')).toBeInTheDocument()
  })
})
