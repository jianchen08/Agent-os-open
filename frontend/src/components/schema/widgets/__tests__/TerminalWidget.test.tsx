/**
 * TerminalWidget 骨架测试 —— 插件接入点（不做真实 PTY）
 *
 * 设计原则：TerminalWidget 本身不实现终端，而是作为「接入点」让插件提供真实能力。
 * - 有 pluginId：渲染插件接入容器（将来插件通过 webview 注入终端 UI）
 * - 无 pluginId：占位提示，引导接入
 *
 * 可观察行为（AC）：
 * - AC-1: 无 pluginId → 占位提示（含「接入」引导文案 + 占位容器）
 * - AC-2: 有 pluginId → 渲染插件接入容器（带 data-testid + pluginId 标识）
 * - AC-3: pluginId 标识透传到容器（便于将来委托渲染定位插件）
 * - AC-4: 可选配置（terminalId/cols/rows/title）不破坏渲染
 */
import { render, screen } from '@testing-library/react'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { TerminalWidget } from '../TerminalWidget'

describe('TerminalWidget 骨架', () => {
  it('AC-1: 无 pluginId → 渲染占位提示，引导接入', () => {
    render(<TerminalWidget />)

    const placeholder = screen.getByTestId('terminal-placeholder')
    expect(placeholder).toBeInTheDocument()
    // 引导文案：必须提到 pluginId / connector 以指明接入路径
    expect(placeholder.textContent).toMatch(/pluginId|connector|接入/)
  })

  it('AC-2: 有 pluginId → 渲染插件接入容器（data-testid）', () => {
    render(<TerminalWidget pluginId="my-terminal-ext" />)

    const host = screen.getByTestId('terminal-plugin-host')
    expect(host).toBeInTheDocument()
    // 不应再渲染「未接入」占位
    expect(screen.queryByTestId('terminal-placeholder')).not.toBeInTheDocument()
  })

  it('AC-3: pluginId 透传到容器，提示「终端由插件 X 提供」', () => {
    render(<TerminalWidget pluginId="xterm-ext" />)

    const host = screen.getByTestId('terminal-plugin-host')
    expect(host.getAttribute('data-plugin-id')).toBe('xterm-ext')
    expect(host.textContent).toContain('xterm-ext')
    expect(host.textContent).toMatch(/插件|plugin/)
  })

  it('AC-4: 可选配置（terminalId/cols/rows/title）不破坏渲染', () => {
    render(
      <TerminalWidget
        pluginId="ext-a"
        terminalId="term-1"
        cols={80}
        rows={24}
        title="Builder"
      />,
    )

    const host = screen.getByTestId('terminal-plugin-host')
    expect(host).toBeInTheDocument()
    // 透传挂载点（将来插件 UI 挂载位置）
    expect(host.querySelector('[data-testid="terminal-plugin-mount"]')).not.toBeNull()
  })
})
