/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * BUG-16 回归 — 现象A：cost_control 类型化表单保存被静默拦死
 *
 * 成本控制配置页的 slider 字段（min:0 max:100，无 step 声明）经 toRjsf 映射出
 * multipleOf 兜底值，历史值 70/90/100 提交时 ajv 校验/编译失败 → 提交被拦且
 * 无 UI 反馈。回归要求：整数值滑条 + 0~1 细步长滑条两场景全链路可编译可提交。
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { describe, expect, it, vi } from 'vitest'
import { RjsfForm, toRjsf } from '../RjsfForm'
import type { UIInputFormField } from '@/types/schema'

/** cost_control plugin.json cost 配置文件的 fields 声明（alerts 三个 slider 无 step） */
const COST_FIELDS: UIInputFormField[] = [
  { name: 'enabled', type: 'toggle', label: '启用成本控制' },
  { name: 'global_config.daily_token_limit', type: 'number', label: '日 token 上限', min: 0 },
  { name: 'alerts.warning_threshold', type: 'slider', label: '预警阈值（%）', min: 0, max: 100 },
  { name: 'alerts.critical_threshold', type: 'slider', label: '严重阈值（%）', min: 0, max: 100 },
  { name: 'alerts.exhausted_threshold', type: 'slider', label: '耗尽阈值（%）', min: 0, max: 100 },
  { name: 'protection.auto_save_at_warning', type: 'toggle', label: '预警时自动保存' },
]

/** 上下文窗口配置的 0~1 细步长滑条（compress_trigger_ratio，无 step 声明） */
const RATIO_FIELD: UIInputFormField[] = [
  { name: 'compress_trigger_ratio', type: 'slider', label: '压缩触发比例', min: 0, max: 1 },
]

describe('BUG-16 现象A — slider 未声明 step 的 multipleOf 按 max 量级自适应', () => {
  it('max>1 的 0~100 整数滑条 → multipleOf: 1（非整粒度约束不上整数区间）', () => {
    const { schema } = toRjsf([
      { name: 'w', type: 'slider', label: 'W', min: 0, max: 100 },
    ])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.w).toMatchObject({ type: 'number', minimum: 0, maximum: 100, multipleOf: 1 })
  })

  it('0~1 细步长滑条（compress_trigger_ratio 场景）不回退 → multipleOf: 0.01', () => {
    const { schema } = toRjsf([
      { name: 'r', type: 'slider', label: 'R', min: 0, max: 1 },
    ])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.r).toMatchObject({ type: 'number', minimum: 0, maximum: 1, multipleOf: 0.01 })
  })

  it('声明了 step 照旧用 step', () => {
    const { schema } = toRjsf([
      { name: 's', type: 'slider', label: 'S', min: 0, max: 100, step: 5 },
    ])
    const props = schema.properties as Record<string, Record<string, unknown>>
    expect(props.s).toMatchObject({ multipleOf: 5 })
  })
})

describe('BUG-16 — 提交被校验拦截必有可见反馈（禁止静默）', () => {
  it('schema 级错误（ajv 编译失败 {stack}，无 property）拦截提交：alert 可见且不静默', async () => {
    const onSubmit = vi.fn()
    // 非法正则 → ajv 编译失败 → errors=[{stack}]，不落任何字段、无就近提示，
    // 修复前 = 零 UI 反馈静默拦截（与 GUI 实测 cost 页签名一致）
    const fields: UIInputFormField[] = [
      { name: 'code', type: 'string', label: '代码', validation: { pattern: '(((' } },
    ]
    const { container } = render(<RjsfForm fields={fields} onSubmit={onSubmit} />)
    fireEvent.submit(container.querySelector('form')!)
    expect(onSubmit).not.toHaveBeenCalled()
    const alert = await screen.findByRole('alert')
    expect(alert.textContent).toContain('保存被表单校验拦截')
    expect(alert.textContent).toContain('Invalid regular expression')
  })

  it('字段级错误（必填缺失）就近提示渲染，错误条不重复展示', async () => {
    const onSubmit = vi.fn()
    const fields: UIInputFormField[] = [
      { name: 'config_id', type: 'string', label: '配置ID', required: true },
    ]
    const { container } = render(<RjsfForm fields={fields} onSubmit={onSubmit} />)
    fireEvent.submit(container.querySelector('form')!)
    expect(onSubmit).not.toHaveBeenCalled()
    // 就近提示可见
    expect(await screen.findByText('配置ID不能为空')).toBeInTheDocument()
    // 全部错误都落在字段上 → 无表单级错误条
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('BUG-16 现象A — 0~100 整数滑条不阻塞提交', () => {
  it('历史值 70/90/100 + 整数上限：全链路可提交，值原样流出', async () => {
    const onSubmit = vi.fn()
    const initialValues = {
      enabled: true,
      'global_config.daily_token_limit': 1000001,
      'alerts.warning_threshold': 70,
      'alerts.critical_threshold': 90,
      'alerts.exhausted_threshold': 100,
      'protection.auto_save_at_warning': true,
    }
    const { container } = render(
      <RjsfForm fields={COST_FIELDS} initialValues={initialValues} onSubmit={onSubmit} />,
    )
    fireEvent.submit(container.querySelector('form')!)
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit.mock.calls[0][0]).toMatchObject({
      enabled: true,
      'global_config.daily_token_limit': 1000001,
      'alerts.warning_threshold': 70,
      'alerts.critical_threshold': 90,
      'alerts.exhausted_threshold': 100,
      'protection.auto_save_at_warning': true,
    })
  })

  it('0~1 细步长滑条（compress_trigger_ratio）历史值 0.85 可提交', async () => {
    const onSubmit = vi.fn()
    const { container } = render(
      <RjsfForm fields={RATIO_FIELD} initialValues={{ compress_trigger_ratio: 0.85 }} onSubmit={onSubmit} />,
    )
    fireEvent.submit(container.querySelector('form')!)
    await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1))
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ compress_trigger_ratio: 0.85 })
  })
})
