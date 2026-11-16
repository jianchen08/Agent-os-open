/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 上下文用量指示器悬停开合补测：鼠标移入圆环打开明细浮窗、从浮窗移出即关闭。
 *
 * 断行为：浮窗节点出现/消失（hover 是无点击场景下唯一的开合路径），以及窗口
 * 未知时不展示假进度条但明细仍可达。
 */

import { fireEvent, render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ContextUsageIndicator } from '../ContextUsageIndicator'

describe('ContextUsageIndicator — 浮窗悬停开合', () => {
  it('悬停圆环打开明细浮窗，移出浮窗即关闭', () => {
    render(
      <ContextUsageIndicator modelName="deepseek-v3" currentTokenUsage={1200} maxTokens={64000} />,
    )

    fireEvent.mouseEnter(screen.getByTestId('context-usage-ring'))
    const popover = screen.getByTestId('context-usage-popover')
    expect(popover).toHaveTextContent('上下文 · deepseek-v3')

    fireEvent.mouseLeave(popover)
    expect(screen.queryByTestId('context-usage-popover')).not.toBeInTheDocument()
  })

  it('窗口未知（maxTokens<=0）时仍可悬停打开明细（不展示假进度条但信息可达）', () => {
    render(<ContextUsageIndicator modelName="m" currentTokenUsage={10} maxTokens={0} />)

    fireEvent.mouseEnter(screen.getByTestId('context-usage-ring'))

    expect(screen.getByTestId('context-usage-popover')).toBeInTheDocument()
    expect(screen.queryByTestId('context-usage-popover-bar')).not.toBeInTheDocument()
  })
})
