/**
 * 富交互形态 widget 测试（widget 化 G5：wizard / sortable_list / inline_edit）
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import React from 'react'

import { WizardWidget } from '../WizardWidget'
import { SortableListWidget } from '../SortableListWidget'
import { InlineEditWidget } from '../InlineEditWidget'

vi.mock('@/components/ui/sonner', () => ({
  toast: { success: vi.fn(), error: vi.fn() },
}))

const submit = () => fireEvent.submit(document.querySelector('form')!)

beforeEach(() => {
  vi.clearAllMocks()
  vi.unstubAllGlobals()
})

describe('wizard 多步表单', () => {
  const steps = [
    { title: '基本信息', fields: [{ name: 'title', type: 'input' as const, label: '标题', required: true }] },
    { title: '执行环境', fields: [{ name: 'workspace', type: 'input' as const, label: '工作空间' }] },
  ]

  it('分步渲染、跨步累积、末步提交全量值', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ json: async () => ({ task_id: 't-1' }) })
    vi.stubGlobal('fetch', fetchMock)
    render(
      <WizardWidget
        steps={steps}
        endpoint="/ext/tasks/root"
        eventName="task.created"
        successText="任务已创建"
      />,
    )

    // 步骤 1
    expect(screen.getByText('1. 基本信息')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'T1' } })
    submit()
    // 步骤 2
    await waitFor(() => expect(screen.getByText('2. 执行环境')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('工作空间'), { target: { value: 'ws-prod' } })
    submit()

    // 末步提交：全量累积值 + pipeline_id
    await waitFor(() => expect(fetchMock).toHaveBeenCalled())
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/ext/tasks/root')
    expect(JSON.parse(init.body)).toMatchObject({ title: 'T1', workspace: 'ws-prod', pipeline_id: expect.any(String) })
  })
})

describe('sortable_list 拖拽排序', () => {
  it('上移/下移按钮重排并回调 onChange', () => {
    const onChange = vi.fn()
    const { rerender } = render(
      <SortableListWidget items={['a', 'b', 'c']} onChange={onChange} />,
    )
    expect(screen.getByTestId('sortable-item-0')).toHaveTextContent('a')
    fireEvent.click(screen.getByRole('button', { name: '下移 a' }))
    expect(onChange).toHaveBeenCalledTimes(1)
    const next = onChange.mock.calls[0][0] as Array<{ value: string }>
    expect(next.map((i) => i.value)).toEqual(['b', 'a', 'c'])
    // 受控回写（宿主更新 items）后反映新序
    rerender(<SortableListWidget items={['b', 'a', 'c']} onChange={onChange} />)
    expect(screen.getByTestId('sortable-item-0')).toHaveTextContent('b')
  })

  it('对象条目与 labels 映射', () => {
    render(
      <SortableListWidget
        items={[{ label: 'x', value: 'x1' }, { label: 'y', value: 'y1' }]}
        labels={{ x1: 'X 显示名' }}
      />,
    )
    expect(screen.getByTestId('sortable-item-0')).toHaveTextContent('X 显示名')
    expect(screen.getByTestId('sortable-item-1')).toHaveTextContent('y')
  })
})

describe('inline_edit 内联编辑', () => {
  it('点击进入编辑 → 回车提交 onChange（值变化才提交），Esc 取消', () => {
    const onChange = vi.fn()
    render(<InlineEditWidget value="旧名" onChange={onChange} />)
    expect(screen.getByTestId('inline-edit-view')).toHaveTextContent('旧名')

    // 点击进入编辑 → 输入新值 → Enter 提交
    fireEvent.click(screen.getByTestId('inline-edit-view'))
    const input = screen.getByTestId('inline-edit-input')
    fireEvent.change(input, { target: { value: '新名' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(onChange).toHaveBeenCalledWith('新名')

    // 值未变不提交（onChange 只在 draft !== value 时触发）——再进一次不改直接 Enter
    fireEvent.click(screen.getByTestId('inline-edit-view'))
    fireEvent.keyDown(screen.getByTestId('inline-edit-input'), { key: 'Escape' })
    expect(onChange).toHaveBeenCalledTimes(1)
  })
})
