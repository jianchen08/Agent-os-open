/**
 * ModalShell 成功自动关闭行为测试（closeOnSuccess × open 联动）
 *
 * 调用方传 closeOnSuccess={status === 'success'}，而 status 不随关闭复位——
 * 自动关闭必须同时响应 closeOnSuccess 与 open 两个量的变化，否则：
 * - 提交成功自动关闭一次后 closeOnSuccess 残留 true，重开弹窗不再自动关；
 * - 连续第二次提交（onSubmit 路径无中间态，status 仍 success）也无法再触发关闭。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it, vi } from 'vitest'
import { FormWidget } from '../FormWidget'

const fields = [{ name: 'title', type: 'input' as const, label: '标题', required: true }]
const submitForm = () => fireEvent.submit(document.querySelector('form')!)

describe('ModalShell：成功自动关闭响应 open 变化', () => {
  it('trigger 自持模式：提交成功自动关闭；closeOnSuccess 残留 true 时重开也自动关闭', async () => {
    const onClose = vi.fn()
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(
      <FormWidget
        modal={{ trigger: '打开表单', title: '新建任务' }}
        fields={fields}
        onSubmit={onSubmit}
        onClose={onClose}
      />,
    )
    // 第一次打开 → 提交成功 → 自动关闭
    fireEvent.click(screen.getByRole('button', { name: '打开表单' }))
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T1' } })
    submitForm()
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    // 重开：closeOnSuccess 残留 true（未迁移），open false→true 须再次触发自动关闭
    fireEvent.click(screen.getByRole('button', { name: '打开表单' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })

  it('受控 open 模式：父组件重开弹窗时，残留成功态自动关闭', async () => {
    const onClose = vi.fn()
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    function Harness() {
      const [open, setOpen] = useState(true)
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>
            重开
          </button>
          <FormWidget
            modal={{ title: '新建任务' }}
            open={open}
            onClose={() => {
              onClose()
              setOpen(false)
            }}
            fields={fields}
            onSubmit={onSubmit}
          />
        </>
      )
    }
    render(<Harness />)
    expect(screen.getByRole('dialog')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T1' } })
    submitForm()
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())

    // 父组件重开（open false→true）：closeOnSuccess 仍为 true → 自动关闭
    fireEvent.click(screen.getByRole('button', { name: '重开' }))
    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
  })
})
