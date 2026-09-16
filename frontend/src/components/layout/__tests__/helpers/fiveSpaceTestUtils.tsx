/**
 * FiveSpaceLayout 测试家族共享基建：ResizeObserver 打桩 / 视口注入 / 固定内容 / 渲染器。
 * 曾在主测试与 gaps 测试逐字复制（jscpd 克隆门禁重复源）。
 */
import { render } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { FiveSpaceLayout } from '../../FiveSpaceLayout'
import React from 'react'

/** antd Splitter 依赖 ResizeObserver / matchMedia，jsdom 缺失，测试环境打桩 */
export class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}

export function setViewportWidth(width: number) {
  Object.defineProperty(window, 'innerWidth', { value: width, writable: true, configurable: true })
}

export const chatContent = <div data-testid="chat-content">对话内容</div>
export const sidebarContent = <div data-testid="sidebar-content">侧栏导航</div>

export function renderLayout() {
  return render(
    <MemoryRouter>
      <FiveSpaceLayout chatContent={chatContent} sidebarContent={sidebarContent} />
    </MemoryRouter>,
  )
}
