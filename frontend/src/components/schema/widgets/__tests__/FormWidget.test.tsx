/**
 * FormWidget 测试（渲染引擎 = RjsfForm，真 antd 渲染）
 *
 * 验证聊天表单 widget 的「分发 + 值收集 + 校验」：
 * - toggle → antd Switch（role=switch），值为 boolean
 * - radio → 单选，值为标量（保留原始值类型）
 * - checkbox / multiselect → 复选组，值为数组
 * - slider → antd Slider
 * - date → antd DatePicker
 * - input / select / number 收集值；required 空数组被拦截
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { DecisionFormAdapter, FormWidget } from '../FormWidget'

/**
 * 提交动作：模拟表单 submit 事件（等价于用户点击 type=submit 按钮在真实浏览器
 * 触发的提交；jsdom 的 click 激活路径不重放该事件，故直接派发 submit）
 */
const submitForm = () => fireEvent.submit(document.querySelector('form')!)

describe('FormWidget — antd 控件类型（toggle/radio/checkbox/multiselect/slider/date）', () => {
  it('toggle 类型渲染 Switch，点击后提交值为 true', () => {
    const onSubmit = vi.fn()
    render(
      <FormWidget
        fields={[{ name: 'auto', type: 'toggle', label: '自动' }]}
        onSubmit={onSubmit}
      />,
    )
    const switchBtn = screen.getByRole('switch')
    expect(switchBtn).toBeInTheDocument()
    fireEvent.click(switchBtn)
    submitForm()
    expect((onSubmit.mock.calls[0][0] as Record<string, unknown>).auto).toBe(true)
  })

  it('radio 单选：值为标量（保留原始值类型）', () => {
    const onSubmit = vi.fn()
    render(
      <FormWidget
        fields={[
          {
            name: 'level',
            type: 'radio',
            label: '级别',
            options: [
              { value: 'low', label: '低' },
              { value: 'high', label: '高' },
            ],
          },
        ]}
        onSubmit={onSubmit}
      />,
    )
    fireEvent.click(screen.getByLabelText('高'))
    submitForm()
    expect((onSubmit.mock.calls[0][0] as Record<string, unknown>).level).toBe('high')
  })

  it('checkbox 多选：值为数组', () => {
    const onSubmit = vi.fn()
    render(
      <FormWidget
        fields={[
          {
            name: 'perms',
            type: 'checkbox',
            label: '权限',
            options: [
              { value: 'read', label: '读' },
              { value: 'write', label: '写' },
            ],
          },
        ]}
        onSubmit={onSubmit}
      />,
    )
    fireEvent.click(screen.getByLabelText('读'))
    fireEvent.click(screen.getByLabelText('写'))
    submitForm()
    expect((onSubmit.mock.calls[0][0] as Record<string, unknown>).perms).toEqual([
      'read',
      'write',
    ])
  })

  it('multiselect 复选组：值收集为数组（选多个）', () => {
    const onSubmit = vi.fn()
    render(
      <FormWidget
        fields={[
          {
            name: 'tags',
            type: 'multiselect',
            label: '标签',
            options: [
              { value: 'a', label: 'Alpha' },
              { value: 'b', label: 'Beta' },
            ],
          },
        ]}
        onSubmit={onSubmit}
      />,
    )
    fireEvent.click(screen.getByLabelText('Alpha'))
    fireEvent.click(screen.getByLabelText('Beta'))
    submitForm()
    expect((onSubmit.mock.calls[0][0] as Record<string, unknown>).tags).toEqual(['a', 'b'])
  })

  it('multiselect / checkbox required 校验：空数组被拦截并显示中文错误', () => {
    const onSubmit = vi.fn()
    render(
      <FormWidget
        fields={[
          {
            name: 'tags',
            type: 'multiselect',
            label: '标签',
            required: true,
            options: [{ value: 'a', label: 'Alpha' }],
          },
        ]}
        onSubmit={onSubmit}
      />,
    )
    submitForm()
    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByText(/不能为空/)).toBeInTheDocument()
  })

  it('slider 渲染 antd Slider，date 渲染 DatePicker 输入', () => {
    render(
      <FormWidget
        fields={[
          { name: 'count', type: 'slider', label: '数量', min: 0, max: 10, step: 1 },
          { name: 'publish', type: 'date', label: '发布日期' },
        ]}
        onSubmit={vi.fn()}
      />,
    )
    expect(screen.getByRole('slider')).toBeInTheDocument()
    expect(screen.getByLabelText('发布日期')).toBeInTheDocument()
  })
})

describe('FormWidget — input / select / number 值收集', () => {
  it('文本与数字收集（number 经 InputNumber 转数字）', () => {
    const onSubmit = vi.fn()
    render(
      <FormWidget
        fields={[
          { name: 'title', type: 'input', label: '标题' },
          { name: 'count', type: 'number', label: '数量' },
        ]}
        onSubmit={onSubmit}
      />,
    )
    fireEvent.change(screen.getByLabelText('标题'), { target: { value: 'hello' } })
    fireEvent.change(screen.getByLabelText('数量'), { target: { value: '42' } })
    submitForm()

    const v = onSubmit.mock.calls[0][0] as Record<string, unknown>
    expect(v.title).toBe('hello')
    expect(v.count).toBe(42)
  })
})

describe('DecisionFormAdapter — decision 注册名 = 单字段表单（原 DecisionWidget 并入）', () => {
  it('options 映射为 radio，点选即经 onDecision 回调（字段模式无提交按钮）', async () => {
    const onDecision = vi.fn()
    render(
      <DecisionFormAdapter
        title="部署方案"
        options={[
          { id: 'a', label: '蓝绿' },
          { id: 'b', label: '灰度' },
        ]}
        onDecision={onDecision}
      />,
    )
    // 标题与 radio 字段 label 同文本——限定标题层级避免 getByText 多命中
    expect(screen.getByRole('heading', { name: '部署方案' })).toBeInTheDocument()
    // 无提交按钮（字段模式）
    expect(screen.queryByRole('button', { name: /提交/ })).not.toBeInTheDocument()
    // 点选 radio → onDecision 立即收到选项 id
    fireEvent.click(screen.getByLabelText('蓝绿'))
    await waitFor(() => expect(onDecision).toHaveBeenCalledWith('a'))
  })

  it('multiple 模式映射为 checkbox，回调收到数组', async () => {
    const onDecision = vi.fn()
    render(
      <DecisionFormAdapter
        options={[
          { id: 'a', label: '选项A' },
          { id: 'b', label: '选项B' },
        ]}
        multiple
        onDecision={onDecision}
      />,
    )
    fireEvent.click(screen.getByLabelText('选项A'))
    await waitFor(() => expect(onDecision).toHaveBeenCalledWith(['a']))
  })

  it('空 options 渲染占位（暂无表单字段）', () => {
    render(<DecisionFormAdapter options={[]} />)
    expect(screen.getByText('暂无表单字段')).toBeInTheDocument()
  })
})
