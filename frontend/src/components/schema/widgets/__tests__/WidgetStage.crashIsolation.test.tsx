/** @feature FP-T12 前端适配 | @ci: frontend-test */
// @feature BUG-1 packtest 监控白屏回归
/**
 * WidgetStage 崩溃隔离测试（监控/触发器等 widget_stage 声明页组）
 *
 * 契约：
 * - 任一声明 widget 渲染抛异常 → 该组降级为错误卡片（可重试），异常不逃逸出
 *   widget_stage——声明页组自隔离，不得炸穿 App 根边界把整页（侧栏/聊天/页签）
 *   卸载（BUG-1：监控页打开整页白屏）；
 * - 崩溃不牵连其他组：tab 切换后正常组照常渲染，重试可恢复；
 * - 监控数据接口残缺形状（空对象 / 504 错误态）在 status_card / table 消费面
 *   只产生占位或错误提示，不抛未捕获异常。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { widgetRegistry } from '@/services/schema/WidgetRegistry'
import { StatusCardWidget } from '../StatusCardWidget'
import { TableWidget } from '../TableWidget'
import { WidgetStage } from '../WidgetStage'

const useDataWidgetMock = vi.hoisted(() => vi.fn())
vi.mock('@/services/schema/dataWidget', async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  useDataWidget: (...args: unknown[]) => useDataWidgetMock(...args),
}))

/** 可控异常源：测试内翻转 boomEnabled 决定挂载的组件是否抛渲染异常 */
let boomEnabled = true
function BoomWidget() {
  const [renderBoom] = useState(() => boomEnabled)
  if (renderBoom) throw new Error('boom: widget render crash')
  return <div data-testid="boom-ok">炸弹组件正常渲染</div>
}

function seedMonitoringWidgets() {
  contributionRegistry.loadFromSchema({
    plugin_contributes: [
      {
        plugin_id: 'seed_monitor',
        plugin_name: 'Seed',
        ui_schema: {
          widgets: [
            { id: 'res_cpu', type: 'status_card', space: 'monitoring', group: '资源', order: 10, props: { title: 'CPU', label: 'CPU', value: 12 } },
            { id: 'boom_widget', type: 'boom', space: 'monitoring', group: '概览', order: 5, props: {} },
          ],
        },
      },
    ],
    plugin_configs: [],
  })
}

beforeEach(() => {
  useDataWidgetMock.mockReset()
  useDataWidgetMock.mockReturnValue({ data: null, loading: false, error: null })
  boomEnabled = true
  widgetRegistry.clear()
  widgetRegistry.register('status_card', StatusCardWidget, {
    name: 'status_card',
    supportedSpaces: ['workspace'],
  })
  widgetRegistry.register('table', TableWidget, {
    name: 'table',
    supportedSpaces: ['workspace'],
  })
  widgetRegistry.register('boom', BoomWidget, {
    name: 'boom',
    supportedSpaces: ['workspace'],
  })
  seedMonitoringWidgets()
})

afterEach(() => {
  widgetRegistry.clear()
  cleanupStaleErrors()
})

/** React 无边界时会把渲染异常重抛到 window.onerror——隔离测试噪音 */
function cleanupStaleErrors() {
  vi.restoreAllMocks()
}

describe('WidgetStage — 崩溃隔离（BUG-1 回归）', () => {
  it('声明 widget 渲染抛异常 → 组内错误卡片，异常不逃逸出 widget_stage', () => {
    // 「概览」组（order 5）先于「资源」组渲染，组内含抛异常 widget
    render(<WidgetStage space="monitoring" />)
    expect(screen.getByTestId('widget-stage-error')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('boom: widget render crash')
    expect(screen.getByTestId('widget-stage-retry')).toBeInTheDocument()
  })

  it('崩溃不牵连其他组：切到正常组后照常渲染其 widget', () => {
    render(<WidgetStage space="monitoring" />)
    fireEvent.click(screen.getByTestId('widget-stage-tab-资源'))
    expect(screen.getByTestId('declared-widget-res_cpu')).toBeInTheDocument()
    expect(screen.queryByTestId('widget-stage-error')).not.toBeInTheDocument()
  })

  it('异常源消除后点重试 → 组内容恢复渲染', () => {
    render(<WidgetStage space="monitoring" />)
    expect(screen.getByTestId('widget-stage-error')).toBeInTheDocument()
    boomEnabled = false
    fireEvent.click(screen.getByTestId('widget-stage-retry'))
    expect(screen.getByTestId('boom-ok')).toBeInTheDocument()
    expect(screen.queryByTestId('widget-stage-error')).not.toBeInTheDocument()
  })
})

describe('监控数据消费面 — 残缺形状不抛未捕获异常（BUG-1 回归）', () => {
  it('status_card：数据接口空对象（llm_usage={} 同族形状）→ 渲染占位值，不抛', () => {
    useDataWidgetMock.mockReturnValue({ data: {}, loading: false, error: null })
    render(
      <StatusCardWidget
        datasourceUri="/ext/monitoring/token-usage"
        valueKey="token_usage.total_tokens"
        title="Token 用量"
        label="累计 Tokens"
      />,
    )
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('status_card：504 错误态 → 显示错误提示，不抛', () => {
    useDataWidgetMock.mockReturnValue({ data: null, loading: false, error: '504 Gateway Timeout' })
    render(
      <StatusCardWidget
        datasourceUri="/ext/monitoring/token-usage"
        valueKey="token_usage.total_tokens"
        title="Token 用量"
        label="累计 Tokens"
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('504 Gateway Timeout')
  })

  it('table：504 错误态且零行列 → 显示错误提示，不抛', () => {
    useDataWidgetMock.mockReturnValue({ data: null, loading: false, error: '504 Gateway Timeout' })
    render(
      <TableWidget
        datasourceUri="/ext/monitoring/tasks"
        title="最近任务"
        pageSize={10}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('504 Gateway Timeout')
  })
})
