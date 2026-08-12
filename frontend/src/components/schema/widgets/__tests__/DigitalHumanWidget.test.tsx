/**
 * DigitalHumanWidget 占位组件测试
 *
 * 背景(架构 ADR §2.1 / §7.6):
 * - 数字人/3D/2D 形象是 workspace 的 widget(注册名 digital_human),不占独立空间。
 * - 现阶段只做「形象加载点」占位,不引入 Live2D/VRM/three.js 等渲染库(0.7.0 的事)。
 * - 占位要设计成「插件接入就生效」的形态:显示 source/connector 信息,
 *   并订阅 widgetEventStore 为将来表情/动作事件推送预留。
 *
 * 可观察行为(AC):
 * - AC-1: 渲染占位(显示「形象」相关标识 + source 信息)
 * - AC-2: source 缺省时仍渲染占位(不崩溃,提示待插件接入)
 * - AC-3: 接收 connector prop 并显示(标识后端形象 Connector 插件名)
 * - AC-4: modelUri 作为 source 别名也能显示(前向兼容 ADR §7.6 的 props 命名)
 * - AC-5: 有 widgetId 时订阅 widgetEventStore,渲染最新事件载荷(若有)
 * - AC-6: 无 widgetId 时不订阅(不读取 store 的 latest)
 */
import { render, screen } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { DigitalHumanWidget } from '../DigitalHumanWidget'
import { useWidgetEventStore } from '@/stores/widgetEventStore'

describe('DigitalHumanWidget — 占位基础设施', () => {
  beforeEach(() => {
    useWidgetEventStore.setState({ events: {}, latest: {} })
  })
  afterEach(() => {
    useWidgetEventStore.setState({ events: {}, latest: {} })
  })

  it('AC-1: 渲染占位并显示 source 信息', () => {
    render(<DigitalHumanWidget source="live2d://model/haru" />)

    // 占位标识(唯一,精确匹配)
    expect(screen.getByText('形象加载点')).toBeInTheDocument()
    // source 信息回显
    expect(screen.getByText(/live2d:\/\/model\/haru/i)).toBeInTheDocument()
    // 「待插件接入」提示(说明现阶段不渲染真实模型)
    expect(screen.getByText(/形象渲染待插件接入/i)).toBeInTheDocument()
  })

  it('AC-2: source 缺省时仍渲染占位(不崩溃,提示待插件接入)', () => {
    render(<DigitalHumanWidget />)

    expect(screen.getByText('形象加载点')).toBeInTheDocument()
    expect(screen.getByText(/形象渲染待插件接入/i)).toBeInTheDocument()
  })

  it('AC-3: 接收 connector prop 并显示(后端形象 Connector 插件名)', () => {
    render(
      <DigitalHumanWidget
        source="vrm://models/ai"
        connector="avatar-live2d-connector"
      />,
    )

    expect(screen.getByText(/avatar-live2d-connector/i)).toBeInTheDocument()
  })

  it('AC-4: modelUri 作为 source 别名也能显示(前向兼容 ADR §7.6 命名)', () => {
    render(<DigitalHumanWidget modelUri="vrm://models/ai" />)

    expect(screen.getByText(/vrm:\/\/models\/ai/i)).toBeInTheDocument()
  })

  it('AC-5: 有 widgetId 时订阅 widgetEventStore,渲染最新事件载荷', () => {
    // 预置一条 widget 事件(模拟内核 widget_event 推送)
    useWidgetEventStore.setState({
      events: {},
      latest: {
        dh_1: {
          widget_id: 'dh_1',
          event: 'expression',
          data: { name: 'smile', intensity: 0.8 },
        },
      },
    })

    render(<DigitalHumanWidget widgetId="dh_1" source="live2d://model/x" />)

    // 渲染最新事件名(为将来表情/动作推送预留的可观察点;事件名在独立 span 内唯一)
    expect(screen.getByText('expression')).toBeInTheDocument()
  })

  it('AC-6: 无 widgetId 时不读取 store latest(不显示事件区)', () => {
    // 预置一条别的事件,确认无 widgetId 时不会被读取展示
    useWidgetEventStore.setState({
      events: {},
      latest: {
        other: {
          widget_id: 'other',
          event: 'expression',
          data: { name: 'smile' },
        },
      },
    })

    render(<DigitalHumanWidget source="live2d://model/x" />)

    // 无 widgetId:不应回显别 widget 的事件(事件名 span 不应出现)
    expect(screen.queryByText('expression')).not.toBeInTheDocument()
  })
})
