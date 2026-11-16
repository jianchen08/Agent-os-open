/**
 * R14 高表单弹层主操作视口可达：modal 壳 footer 钉住提交钮契约
 *
 * 覆盖：
 * - modal 态主操作提交钮在 footer（type=submit + form 关联表单元素），表单内
 *   不再有第二颗提交钮（防双钮）；
 * - 提交进行中 footer 钮禁用并显示提交中文案（原表单内钮的 loading 语义迁移）；
 * - 提交反馈（form-widget-status）渲染在 footer 而非表单体末尾——footer 钮
 *   触发的提交反馈必须与钮同屏可见；
 * - 提交流本身不变：footer 关联的原生 submit 语义由 form 元素承接
 *   （fireEvent.submit(form) 与浏览器点击 form 关联钮走同一 submit 事件）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { FormWidget } from '../FormWidget'

vi.mock('@/hooks/queries/useSessionsQuery', () => ({
  useSessionsQuery: () => ({ data: [] }),
}))

const apiGet = vi.fn()
const apiPost = vi.fn()
const apiRequest = vi.fn()
vi.mock('@/services/api/client', () => ({
  default: Object.assign(
    (...args: unknown[]) => apiRequest(...args),
    {
      get: (...args: unknown[]) => apiGet(...args),
      post: (...args: unknown[]) => apiPost(...args),
    },
  ),
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

const fields = [{ name: 'title', type: 'input' as const, label: '标题', required: true }]

beforeEach(() => {
  apiGet.mockReset()
  apiPost.mockReset()
  apiRequest.mockReset()
})

describe('R14：modal 壳 footer 提交钮', () => {
  it('表单内无提交钮；footer 钮 type=submit 且 form 关联到渲染出的表单元素 id', () => {
    render(<FormWidget modal={{ title: '新建任务' }} open fields={fields} onSubmit={vi.fn()} />)
    // 表单内不再渲染提交钮（隐藏语义迁移到 footer）
    expect(document.querySelector('form')!.querySelector('button[type="submit"]')).toBeNull()
    const footerBtn = screen.getByTestId('formwidget-modal-submit')
    expect(footerBtn).toHaveAttribute('type', 'submit')
    const formId = footerBtn.getAttribute('form')
    expect(formId).toBeTruthy()
    // form 关联的表单元素真实存在（原生 submit 语义的落点）
    expect(document.getElementById(formId!)).not.toBeNull()
    // 取消钮仍在 footer
    expect(screen.getByRole('button', { name: '取消' })).toBeInTheDocument()
  })

  it('footer 钮提交中禁用并显示提交中文案；反馈状态渲染在 footer', async () => {
    let resolveSubmit: () => void = () => {}
    const onSubmit = vi.fn().mockImplementation(
      () => new Promise<void>((resolve) => { resolveSubmit = resolve }),
    )
    const onClose = vi.fn()
    render(
      <FormWidget modal={{ title: '新建任务' }} open fields={fields} onSubmit={onSubmit} onClose={onClose} submitLabel="创建" />,
    )
    const footerBtn = screen.getByTestId('formwidget-modal-submit')
    expect(footerBtn).toHaveTextContent('创建')
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T1' } })
    fireEvent.submit(document.querySelector('form')!)
    await waitFor(() => expect(onSubmit).toHaveBeenCalled())
    // 提交进行中：禁用 + 提交中文案
    await waitFor(() => expect(footerBtn).toBeDisabled())
    expect(footerBtn).toHaveTextContent('提交中')
    // 状态反馈行在 footer（与提交钮同屏），不在表单体末尾的滚动区外
    const status = screen.getByTestId('form-widget-status')
    expect(footerBtn.parentElement).not.toBeNull()
    expect(footerBtn.parentElement!.contains(status)).toBe(true)
    resolveSubmit()
    // 成功边沿：closeOnSuccess 回调 onClose（受控模式由宿主收口关弹窗）
    await waitFor(() => expect(onClose).toHaveBeenCalled())
  })

  it('endpoint 模式失败：失败原因（form-widget-status）落在 footer 内且弹窗保持打开', async () => {
    apiPost.mockResolvedValue({ data: { error: '目标管道不存在' } })
    render(
      <FormWidget modal={{ title: '触发表单' }} open fields={fields} endpoint="/ext/x/trigger" />,
    )
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T' } })
    fireEvent.submit(document.querySelector('form')!)
    await waitFor(() => expect(apiPost).toHaveBeenCalled())
    const status = await screen.findByTestId('form-widget-status')
    expect(status).toHaveTextContent('目标管道不存在')
    // footer 内可见（与提交钮同容器：footer > 状态容器 > 状态行）
    expect(status.parentElement!.parentElement).toBe(
      screen.getByTestId('formwidget-modal-submit').parentElement,
    )
    expect(screen.getByText('触发表单')).toBeInTheDocument()
  })
})
