// @feature: FP-T12 前端适配(卡数据注入) | @ci: frontend-test
// @feature: message-segments P0 | @ci: frontend-test
/**
 * 消息卡宿主桥（宿主 → 卡）：PluginMessageCard message prop 透传层。
 *
 * 契约（web/cards/compression.html 文件头输入契约）：卡以 method "message.data"
 * 接收 params = { message: { content, metadata } }。宿主侧 PluginMessageCard
 * 接 message prop（content + metadata 含 compression_ref），以 injectMessage
 * 透传给 WebviewWidget；widget_id 坐标保持 ${message.id}:${styleId} 不变。
 * （WebviewWidget 下行信封的推送时序见 WebviewWidget.messageData.test.tsx。）
 */
import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'

const webviewProps: Array<Record<string, unknown>> = []
vi.mock('@/components/schema/widgets/WebviewWidget', () => ({
  WebviewWidget: (props: Record<string, unknown>) => {
    webviewProps.push(props)
    return <div data-testid="stub-webview" />
  },
}))

import { PluginMessageCard } from '../PluginMessageCard'

describe('PluginMessageCard — message prop 透传', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    webviewProps.length = 0
    // 样式声明在册（resolveMessageStyle 命中是透传的前置门）
    contributionRegistry.clear()
    contributionRegistry.register({
      type: 'chatMessages',
      id: 'compression_card',
      title: '上下文压缩卡',
      pluginId: 'context_window_guard',
    } as never)
  })

  it('带 message → 以 injectMessage 透传给 WebviewWidget（content + metadata 原样）', () => {
    const metadata = { message_style: 'compression_card', compression_ref: { segment_id: 'seg-1' } }
    render(
      <PluginMessageCard
        instanceKey="mc-1"
        styleId="compression_card"
        message={{ content: '<compressed>…</compressed>', metadata }}
      />,
    )
    expect(screen.getByTestId('stub-webview')).toBeInTheDocument()
    expect(webviewProps[0]?.injectMessage).toEqual({
      content: '<compressed>…</compressed>',
      metadata,
    })
    // widget_id 契约：${message.id}:${styleId}（既有通用事件通道坐标不变）
    expect(webviewProps[0]?.widgetId).toBe('mc-1:compression_card')
  })

  it('不带 message → 不注入（通用 widget 卡自行经 widget.event 取数）', () => {
    render(<PluginMessageCard instanceKey="m-2" styleId="rich_card" />)
    expect(webviewProps[0]?.injectMessage).toBeUndefined()
  })
})
