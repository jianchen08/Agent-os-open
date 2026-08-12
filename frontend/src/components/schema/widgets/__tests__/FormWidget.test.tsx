/**
 * FormWidget 测试 — antd 控件类型扩展（对齐 SchemaDriver）
 *
 * 背景：SchemaDriver 已支持 date/multiselect 等类型，FormWidget 落后。
 * 本测试验证 FormWidget 新增 4 个 antd 控件类型后的「分发 + 值收集 + 序列化」：
 * - date     → antd DatePicker，提交时值序列化为 ISO string（非 dayjs 对象）
 * - multiselect → antd Select mode="multiple"，值为数组
 * - radio    → antd Radio.Group，值为标量（单选）
 * - checkbox → antd Checkbox.Group，值为数组（多选）
 *
 * antd + jsdom 测试策略：mock antd 的四个组件为「忠实于 antd onChange 签名」的薄壳：
 * - 渲染可查询的元素（data-testid / role / label 关联）
 * - 按 antd 真实 onChange 形态触发回调（DatePicker(dayjs, str)、Select(array)、
 *   Radio.Group({target:{value}})、Checkbox.Group(array)）
 * 这样既绕开 antd 在 jsdom 的 portal/动画复杂度，又能精确验证 FormWidget 的
 * 分发与值适配逻辑。FormWidget.tsx 对真实 antd 的类型由 tsc --noEmit 保证。
 *
 * 现有 7 个原生类型（input/select/number/...）不回归。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'

import { FormWidget } from '../FormWidget'

// ── Mock antd：薄壳组件，忠实模拟 antd onChange 签名 ──
// ConfigProvider：直通 children（不引入额外 DOM）
// DatePicker：value 为 dayjs 或 null；onChange(date: dayjs|null, dateString)
// Select(mode=multiple)：value 为数组；onChange(valueArray)
// Radio.Group：options 驱动；onChange({ target: { value } })
// Checkbox.Group：options 驱动；onChange(checkedValueArray)
vi.mock('antd', () => ({
  ConfigProvider: ({ children }: { children?: React.ReactNode }) =>
    children ?? null,
  DatePicker: (props: {
    value?: { format?: (fmt: string) => string } | null
    onChange?: (value: { toISOString: () => string } | null, dateString: string) => void
    placeholder?: string
  }) => (
    <div data-testid="antd-datepicker">
      <input
        type="date"
        value={props.value && props.value.format ? props.value.format('YYYY-MM-DD') : ''}
        onChange={(e) => {
          if (!props.onChange) return
          const raw = e.target.value
          if (!raw) {
            props.onChange(null, '')
            return
          }
          // 构造 dayjs-like 对象（含 toISOString / format），模拟真实 antd 回调
          const fakeDayjs = {
            format: () => raw,
            toISOString: () => raw + 'T00:00:00.000Z',
            isValid: () => true,
          }
          props.onChange(fakeDayjs as unknown as { toISOString: () => string }, raw)
        }}
        placeholder={props.placeholder}
      />
    </div>
  ),
  Select: (props: {
    value?: unknown[]
    onChange?: (value: unknown[]) => void
    options?: Array<{ label: string; value: string | number }>
    mode?: string
  }) => {
    const current = Array.isArray(props.value) ? props.value : []
    return (
      <div data-testid="antd-select" data-mode={props.mode}>
        {(props.options ?? []).map((o) => (
          <label key={String(o.value)}>
            <input
              type="checkbox"
              checked={current.includes(o.value)}
              onChange={(e) => {
                const next = e.target.checked
                  ? [...current, o.value]
                  : current.filter((v) => v !== o.value)
                props.onChange?.(next)
              }}
            />
            {o.label}
          </label>
        ))}
      </div>
    )
  },
  Radio: {
    Group: (props: {
      value?: unknown
      onChange?: (e: { target: { value: string | number } }) => void
      options?: Array<{ label: string; value: string | number }>
      name?: string
    }) => (
      <div data-testid="antd-radio-group" role="radiogroup">
        {(props.options ?? []).map((o) => (
          <label key={String(o.value)}>
            <input
              type="radio"
              name={props.name ?? 'antd-radio'}
              value={String(o.value)}
              checked={props.value === o.value}
              onChange={() => props.onChange?.({ target: { value: o.value } })}
            />
            {o.label}
          </label>
        ))}
      </div>
    ),
  },
  Checkbox: {
    Group: (props: {
      value?: unknown[]
      onChange?: (checkedValue: unknown[]) => void
      options?: Array<{ label: string; value: string | number }>
    }) => {
      const current = Array.isArray(props.value) ? props.value : []
      return (
        <div data-testid="antd-checkbox-group">
          {(props.options ?? []).map((o) => (
            <label key={String(o.value)}>
              <input
                type="checkbox"
                checked={current.includes(o.value)}
                onChange={(e) => {
                  const next = e.target.checked
                    ? [...current, o.value]
                    : current.filter((v) => v !== o.value)
                  props.onChange?.(next)
                }}
              />
              {o.label}
            </label>
          ))}
        </div>
      )
    },
  },
}))

/** 提交按钮（FormWidget 默认 submitLabel='提交'） */
const getSubmit = () => screen.getByRole('button', { name: /提交/ })

describe('FormWidget — antd 控件扩展（date / multiselect / radio / checkbox）', () => {
  it('date 类型渲染 antd DatePicker', () => {
    render(
      <FormWidget
        fields={[{ name: 'publish', type: 'date', label: '发布日期' }]}
        onSubmit={vi.fn()}
      />,
    )
    expect(screen.getByTestId('antd-datepicker')).toBeInTheDocument()
  })

  it('multiselect 类型渲染 antd Select（mode=multiple）并下放选项', () => {
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
        onSubmit={vi.fn()}
      />,
    )
    const sel = screen.getByTestId('antd-select')
    expect(sel).toBeInTheDocument()
    expect(sel).toHaveAttribute('data-mode', 'multiple')
    // 选项已透传给 antd
    expect(screen.getByLabelText('Alpha')).toBeInTheDocument()
    expect(screen.getByLabelText('Beta')).toBeInTheDocument()
  })

  it('radio 类型渲染 antd Radio.Group 并下放选项', () => {
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
        onSubmit={vi.fn()}
      />,
    )
    expect(screen.getByTestId('antd-radio-group')).toBeInTheDocument()
    expect(screen.getByLabelText('低')).toBeInTheDocument()
    expect(screen.getByLabelText('高')).toBeInTheDocument()
  })

  it('checkbox 类型渲染 antd Checkbox.Group 并下放选项', () => {
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
        onSubmit={vi.fn()}
      />,
    )
    expect(screen.getByTestId('antd-checkbox-group')).toBeInTheDocument()
    expect(screen.getByLabelText('读')).toBeInTheDocument()
    expect(screen.getByLabelText('写')).toBeInTheDocument()
  })

  it('multiselect 值收集为数组（选多个 → 提交值是数组）', () => {
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
    fireEvent.click(getSubmit())

    expect(onSubmit).toHaveBeenCalledTimes(1)
    expect((onSubmit.mock.calls[0][0] as Record<string, unknown>).tags).toEqual(['a', 'b'])
  })

  it('radio 单选：值为标量（选一个）', () => {
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
    fireEvent.click(getSubmit())

    expect((onSubmit.mock.calls[0][0] as Record<string, unknown>).level).toBe('high')
  })

  it('checkbox 多选：值为数组（选多个）', () => {
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
    fireEvent.click(getSubmit())

    expect((onSubmit.mock.calls[0][0] as Record<string, unknown>).perms).toEqual([
      'read',
      'write',
    ])
  })

  it('date 提交时序列化为 ISO string（非 dayjs 对象）', () => {
    const onSubmit = vi.fn()
    render(
      <FormWidget
        fields={[{ name: 'publish', type: 'date', label: '发布日期' }]}
        onSubmit={onSubmit}
      />,
    )
    // 模拟用户在 DatePicker 选了 2026-03-20（mock 会以 dayjs-like 触发 onChange）
    const input = screen.getByTestId('antd-datepicker').querySelector('input')!
    fireEvent.change(input, { target: { value: '2026-03-20' } })
    fireEvent.click(getSubmit())

    const val = (onSubmit.mock.calls[0][0] as Record<string, unknown>).publish
    expect(typeof val).toBe('string') // 关键：不是 dayjs 对象
    expect(val).toBe('2026-03-20T00:00:00.000Z')
  })

  it('date 初始值：ISO 字符串经 dayjs 转换后回显', () => {
    render(
      <FormWidget
        fields={[
          {
            name: 'publish',
            type: 'date',
            label: '发布日期',
            default: '2026-01-15T00:00:00.000Z',
          },
        ]}
        onSubmit={vi.fn()}
      />,
    )
    // FormWidget 应把 ISO → dayjs 后传给 DatePicker.value；mock 用 dayjs.format 回显
    const input = screen.getByTestId('antd-datepicker').querySelector('input')!
    expect(input).toHaveValue('2026-01-15')
  })

  it('multiselect / checkbox required 校验：空数组被拦截', () => {
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
    fireEvent.click(getSubmit())
    expect(onSubmit).not.toHaveBeenCalled()
    expect(screen.getByText(/不能为空/)).toBeInTheDocument()
  })

  it('checkbox required 空数组被拦截', () => {
    const onSubmit = vi.fn()
    render(
      <FormWidget
        fields={[
          {
            name: 'perms',
            type: 'checkbox',
            label: '权限',
            required: true,
            options: [{ value: 'read', label: '读' }],
          },
        ]}
        onSubmit={onSubmit}
      />,
    )
    fireEvent.click(getSubmit())
    expect(onSubmit).not.toHaveBeenCalled()
  })
})

describe('FormWidget — 现有 7 个原生类型不回归', () => {
  it('input / select / number / toggle 渲染并收集值', () => {
    const onSubmit = vi.fn()
    render(
      <FormWidget
        fields={[
          { name: 'title', type: 'input', label: '标题' },
          {
            name: 'kind',
            type: 'select',
            label: '种类',
            options: [{ value: 'x', label: 'X' }],
          },
          { name: 'count', type: 'number', label: '数量' },
        ]}
        onSubmit={onSubmit}
      />,
    )

    // antd mock 不应出现在这些字段里（确保未被错误分发）
    expect(screen.queryByTestId('antd-datepicker')).not.toBeInTheDocument()
    expect(screen.queryByTestId('antd-select')).not.toBeInTheDocument()
    expect(screen.queryByTestId('antd-radio-group')).not.toBeInTheDocument()
    expect(screen.queryByTestId('antd-checkbox-group')).not.toBeInTheDocument()

    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'hello' } })
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'x' } })
    fireEvent.change(screen.getByRole('spinbutton'), { target: { value: '42' } })
    fireEvent.click(getSubmit())

    const v = onSubmit.mock.calls[0][0] as Record<string, unknown>
    expect(v.title).toBe('hello')
    expect(v.kind).toBe('x')
    expect(v.count).toBe(42)
  })
})
