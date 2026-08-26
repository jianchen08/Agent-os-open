/** @feature 统一错误模型 | @ci: frontend-test */
/**
 * ErrorSourceBadge 来源标签（config/error_codes.json sources.enum 单一真值源）：
 * 五种来源渲染对应文案；未知来源兜底「未知」灰标（旧后端兼容）。
 */
import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import { ErrorSourceBadge } from '../ErrorSourceBadge'

describe('ErrorSourceBadge 来源标签（2026-08-26）', () => {
  it('五种来源渲染对应中文标签', () => {
    const cases: Array<[string, string]> = [
      ['kernel', '内核'],
      ['plugin', '插件'],
      ['llm', '模型'],
      ['infra', '网络'],
      ['frontend', '前端'],
    ]
    for (const [source, label] of cases) {
      const { unmount } = render(<ErrorSourceBadge source={source} />)
      expect(screen.getByTestId('error-source-badge').textContent).toBe(label)
      unmount()
    }
  })

  it('未知来源渲染「未知」灰标（旧后端无 source 字段兼容）', () => {
    render(<ErrorSourceBadge />)
    expect(screen.getByTestId('error-source-badge').textContent).toBe('未知')
  })

  it('非法来源字符串同样兜底「未知」', () => {
    render(<ErrorSourceBadge source={'hacker' as any} />)
    expect(screen.getByTestId('error-source-badge').textContent).toBe('未知')
  })
})
