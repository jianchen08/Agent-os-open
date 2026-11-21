/** @feature FP-RP面板v3.1刀5 内容卡 chromeless 声明与卡体自报高度 | @ci: frontend-test */
/**
 * ContentCardRenderer · chromeless 声明 + webview 卡体自报高度测试
 * （2026-10-25 用户实测：卡片带头部件 + 固定 h-80 卡体折叠态留大片空白）。
 *
 * 契约：chromeless=true → 无卡头（标题/来源/源码视图切换）无边框，卡体即全部；
 * 缺省 false = 既有带头卡零变化。webview 卡体注入 ResizeObserver 自报高度桥
 * （宿主脚本，内容为不可信模型输出不会自带），上行 webview.resize 经实例令牌
 * 校验后容器贴合内容（clamp [24, 4000]）。
 */
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { ContentCardRenderer } from '../ContentCardRenderer'

const webviewCard = {
  id: 'todo_list_html',
  match: { fences: ['todo'] },
  form: 'webview' as const,
  pluginId: 'pipeline_todo_track',
}

describe('ContentCardRenderer — chromeless 声明', () => {
  it('chromeless=true：无卡头无边框，webview 卡体带自报高度桥', () => {
    const { container } = render(
      <ContentCardRenderer card={{ ...webviewCard, chromeless: true }} content="<details>任务清单</details>" defaultView={<div>src</div>} />,
    )
    expect(screen.queryByTestId('content-card-toggle')).toBeNull()
    expect(container.querySelector('[data-content-card]')?.className).not.toContain('border')
    const iframe = container.querySelector('iframe')!
    expect(iframe).toBeTruthy()
    expect(iframe.getAttribute('srcdoc')).toContain('webview.resize')
    expect(iframe.getAttribute('srcdoc')).toContain('<details>任务清单</details>')
  })

  it('缺省 false：带头卡零变化（标题/来源/切换条在场）', async () => {
    render(
      <ContentCardRenderer card={{ ...webviewCard }} content="<details>x</details>" defaultView={<div>src</div>} />,
    )
    expect(screen.getByTestId('content-card-toggle')).toBeTruthy()
    expect(screen.getByText('todo_list_html')).toBeTruthy()
  })

  it('chromeless 忽略 default_collapsed（无切换条，恒渲染卡体）', () => {
    render(
      <ContentCardRenderer
        card={{ ...webviewCard, chromeless: true, default_collapsed: true }}
        content="<details>x</details>"
        defaultView={<div>src-view</div>}
      />,
    )
    expect(screen.queryByText('src-view')).toBeNull()
    expect(document.querySelector('iframe')).toBeTruthy()
  })
})

describe('ContentCardRenderer — webview 卡体自报高度桥', () => {
  it('合法 webview.resize 上行（实例令牌）→ 容器贴合高度；出界夹取', async () => {
    const { container } = render(
      <ContentCardRenderer card={{ ...webviewCard, chromeless: true }} content="x" defaultView={<div>src</div>} />,
    )
    const iframe = container.querySelector('iframe')!
    const srcdoc = iframe.getAttribute('srcdoc')!
    const token = /var TOKEN = "([^"]+)"/.exec(srcdoc)![1]

    const post = (height: unknown, origin = 'null') =>
      window.dispatchEvent(
        new MessageEvent('message', {
          origin,
          data: { __agentos_webview: true, __wv_token: token, method: 'webview.resize', params: { height } },
        }),
      )
    const wrapper = iframe.parentElement!
    post(180)
    await waitFor(() => expect(wrapper.style.height).toBe('180px'))
    post(999_999)
    await waitFor(() => expect(wrapper.style.height).toBe('4000px'))
    post(-5)
    await waitFor(() => expect(wrapper.style.height).toBe('24px'))
  })

  it('令牌不匹配 / 非法高度 → 零状态变更（fail-closed）', async () => {
    const { container } = render(
      <ContentCardRenderer card={{ ...webviewCard, chromeless: true }} content="x" defaultView={<div>src</div>} />,
    )
    const wrapper = container.querySelector('iframe')!.parentElement!
    // fail-closed 契约 = 非法消息不改状态：断言前后高度不变（初始占位值随占位
    // 策略演进，不硬编码——2026-11-07 占位高度 24→320 持久化根修）
    const before = wrapper.style.height
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: 'null',
        data: { __agentos_webview: true, __wv_token: 'forged', method: 'webview.resize', params: { height: 500 } },
      }),
    )
    window.dispatchEvent(
      new MessageEvent('message', {
        origin: 'evil',
        data: { __agentos_webview: true, method: 'webview.resize', params: { height: 500 } },
      }),
    )
    await new Promise((r) => setTimeout(r, 50))
    expect(wrapper.style.height).toBe(before)
  })
})

describe('ContentCardRenderer — chromeless template 形态', () => {
  it('template 卡 chromeless：无卡头，模板 {{content}} 正常装配', () => {
    const { container } = render(
      <ContentCardRenderer
        card={{
          id: 'todo_list',
          match: { fences: [], blocks: ['task-list'] },
          form: 'template',
          chromeless: true,
          pluginId: 'pipeline_todo_track',
          template: '<pre>{{content}}</pre>',
        }}
        content="- [ ] 甲"
        defaultView={<div>src</div>}
      />,
    )
    expect(screen.queryByTestId('content-card-toggle')).toBeNull()
    expect(container.querySelector('pre')?.textContent).toContain('- [ ] 甲')
  })

  it('template 卡点击切换条可折叠回源码视图（既有行为保真）', () => {
    const { container } = render(
      <ContentCardRenderer
        card={{
          id: 'todo_list',
          match: { fences: [], blocks: ['task-list'] },
          form: 'template',
          pluginId: 'p',
          template: '<pre>{{content}}</pre>',
        }}
        content="- [ ] 甲"
        defaultView={<div>src-view</div>}
      />,
    )
    fireEvent.click(screen.getByTestId('content-card-toggle'))
    expect(container.textContent).toContain('src-view')
  })
})
