/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * 内容卡 webview 形态的离屏懒挂载补测：元素不可见时不建 iframe（长会话性能
 * §8），只留等高占位；可见后正常挂载沙箱 iframe。
 *
 * mock 边界：元素可见性判定（IntersectionObserver 边界），其余链路真实。
 */

import { render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const visibleState = vi.hoisted(() => ({ visible: false }))
vi.mock('@/hooks/useElementVisible', () => ({
  useElementVisible: () => ({ ref: () => {}, visible: visibleState.visible }),
}))

import { ContentCardRenderer } from '../ContentCardRenderer'
import type { ContentCardDeclaration } from '@/services/schema/contentCard'

const webviewCard: ContentCardDeclaration = {
  id: 'html_preview',
  match: { fences: ['html'] },
  form: 'webview',
  pluginId: 'host',
}

beforeEach(() => {
  visibleState.visible = false
})

describe('ContentCardRenderer — webview 卡离屏懒挂载', () => {
  it('不可见 → 不建 iframe，只渲染等高占位', () => {
    render(
      <ContentCardRenderer card={webviewCard} content="<h1>hi</h1>" defaultView={<pre>源码</pre>} />,
    )

    expect(document.querySelector('iframe')).toBeNull()
    expect(screen.queryByTitle('content-card-preview')).not.toBeInTheDocument()
  })

  it('可见 → 挂载沙箱 iframe（不开 allow-same-origin）', async () => {
    visibleState.visible = true
    render(
      <ContentCardRenderer card={webviewCard} content="<h1>hi</h1>" defaultView={<pre>源码</pre>} />,
    )

    const frame = await screen.findByTitle('content-card-preview')
    expect(frame).toBeInTheDocument()
    expect(frame.getAttribute('sandbox') ?? '').not.toContain('allow-same-origin')
  })
})
