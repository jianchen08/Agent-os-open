/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：ContextUsageIndicator 上下文用量指示器（圈型进度）
 *
 * 用户决策：输入框上下文用量由横向进度条改为圈型进度（AI app 标准，如
 * ChatGPT/Claude 的 token 圆环）——节省横向空间（防发送按钮被挤出）且信息密度不变。
 *
 * 语义沿用：>=90% error 红 / >=70% warning 黄 / 其余 success 绿；
 * maxTokens<=0 不展示假进度；compact（小尺寸）同样渲染圆环。
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ContextUsageIndicator } from '../ContextUsageIndicator'

describe('ContextUsageIndicator — 圈型进度', () => {
  it('maxTokens>0 时渲染圆环进度（role=progressbar，值正确），不再渲染横向条', () => {
    render(
      <ContextUsageIndicator modelName="deepseek-v3" currentTokenUsage={4000} maxTokens={10000} />,
    )

    const ring = screen.getByRole('progressbar')
    expect(ring).toBeInTheDocument()
    expect(ring).toHaveAttribute('aria-valuenow', '40')
    expect(ring).toHaveAttribute('aria-valuemin', '0')
    expect(ring).toHaveAttribute('aria-valuemax', '100')
    // 旧横向进度条结构已移除
    expect(screen.queryByTestId('context-usage-bar')).not.toBeInTheDocument()
    // 具体数值不常驻（只在悬停 title 提示中显示）
    expect(screen.queryByText('4,000')).not.toBeInTheDocument()
    expect(screen.queryByText('10,000')).not.toBeInTheDocument()
    // title 悬停提示携带具体数值
    const indicator = screen.getByTestId('context-usage-indicator')
    expect(indicator).toHaveAttribute('title', '上下文 4,000 / 10,000')
  })

  it('悬停 title 包含总 token 与缓存命中详情（有值才显示段）', () => {
    render(
      <ContextUsageIndicator
        modelName="deepseek-v3"
        currentTokenUsage={4000}
        maxTokens={10000}
        totalTokens={6500}
        cachedTokens={2880}
        hitRatio={0.72}
      />,
    )

    const indicator = screen.getByTestId('context-usage-indicator')
    expect(indicator).toHaveAttribute(
      'title',
      '上下文 4,000 / 10,000 · 总 6,500 tok · 缓存命中 2,880 tok',
    )
  })

  it('无缓存 token 但有命中率时显示命中率；无详情数据时 title 仅模型名', () => {
    const { rerender } = render(
      <ContextUsageIndicator
        modelName="deepseek-v3"
        currentTokenUsage={4000}
        maxTokens={10000}
        totalTokens={6500}
        hitRatio={0.72}
      />,
    )
    expect(screen.getByTestId('context-usage-indicator')).toHaveAttribute(
      'title',
      '上下文 4,000 / 10,000 · 总 6,500 tok · 缓存命中率 72%',
    )

    rerender(<ContextUsageIndicator modelName="deepseek-v3" maxTokens={0} />)
    expect(screen.getByTestId('context-usage-indicator')).toHaveAttribute('title', 'deepseek-v3')
  })

  it('圆环颜色语义：>=90% error / >=70% warning / 其余 success', () => {
    // 进度圆环 = 第二个 circle（第一个是轨道）；SVG className 为 SVGAnimatedString，用 class 属性断言
    const progressCircleClass = () =>
      screen.getByRole('progressbar').querySelectorAll('circle')[1].getAttribute('class') ?? ''

    const { rerender } = render(
      <ContextUsageIndicator modelName="m" currentTokenUsage={9500} maxTokens={10000} />,
    )
    expect(progressCircleClass()).toContain('text-status-error')

    rerender(<ContextUsageIndicator modelName="m" currentTokenUsage={7500} maxTokens={10000} />)
    expect(progressCircleClass()).toContain('text-status-warning')

    rerender(<ContextUsageIndicator modelName="m" currentTokenUsage={3000} maxTokens={10000} />)
    expect(progressCircleClass()).toContain('text-status-success')
  })

  it('maxTokens<=0 不渲染假进度（无 progressbar）', () => {
    render(<ContextUsageIndicator modelName="deepseek-v3" currentTokenUsage={0} maxTokens={0} />)
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
    expect(screen.getByTestId('context-usage-indicator')).toBeInTheDocument()
  })

  it('模型无效时显示占位，不渲染圆环', () => {
    render(<ContextUsageIndicator modelName="unknown" />)
    expect(screen.getByTestId('context-usage-invalid')).toBeInTheDocument()
    expect(screen.queryByRole('progressbar')).not.toBeInTheDocument()
  })

  it('compact 模式（小尺寸）同样渲染圆环', () => {
    render(
      <ContextUsageIndicator compact modelName="m" currentTokenUsage={1000} maxTokens={2000} />,
    )
    expect(screen.getByRole('progressbar')).toBeInTheDocument()
    expect(screen.getByRole('progressbar')).toHaveAttribute('aria-valuenow', '50')
  })
})
