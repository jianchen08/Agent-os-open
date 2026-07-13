/**
 * 组件测试环境验证
 *
 * 验证 jsdom + React render + jest-dom 匹配器 能正常工作
 */
import { render, screen } from '@testing-library/react'
import { describe, it, expect } from 'vitest'

/** 最简单的函数组件，用于验证 render */
function Hello({ name }: { name: string }) {
  return <div data-testid="greeting">Hello, {name}!</div>
}

describe('组件测试环境验证', () => {
  it('jsdom + React render 正常工作', () => {
    render(<Hello name="Vitest" />)
    const el = screen.getByTestId('greeting')
    expect(el).toBeInTheDocument()
    expect(el.textContent).toBe('Hello, Vitest!')
  })

  it('jest-dom 扩展匹配器可用', () => {
    render(<Hello name="Test" />)
    // toBeVisible 来自 @testing-library/jest-dom 扩展
    expect(screen.getByText('Hello, Test!')).toBeVisible()
  })
})
