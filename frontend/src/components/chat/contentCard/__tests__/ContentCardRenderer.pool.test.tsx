/** @feature FP-RP面板v3.1刀5 | @ci: frontend-test */
/**
 * 内容卡 webview 形态全流渲染池补测（用户裁定 2026-11-07）：池成员 = 整条消息
 * 流的全部卡，裁决 = 距视口距离排序，只挂最近 WEBVIEW_CARD_POOL_LIMIT 张；
 * 已挂卡滚出视窗不卸载（不闪不重载），跌出最近 N（太远）才卸；卸载/重挂载
 * 因容器高度持久化而零布局变化。
 *
 * mock 边界：无（jsdom 无 IntersectionObserver 不影响池链路——池不依赖 IO）。
 * 距离桩用实例级 getBoundingClientRect 替身注入远/近位。
 */

import { act, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ContentCardRenderer } from '../ContentCardRenderer'
import type { ContentCardDeclaration } from '@/services/schema/contentCard'

const webviewCard: ContentCardDeclaration = {
  id: 'html_preview',
  match: { fences: ['html'] },
  form: 'webview',
  pluginId: 'host',
}

function cardList(count: number, skipIndex = -1) {
  return (
    <div>
      {Array.from({ length: count }, (_, i) =>
        i === skipIndex ? null : (
          <ContentCardRenderer
            key={`card-${i}`}
            card={webviewCard}
            content={`<p>${i}</p>`}
            defaultView={<pre>源码</pre>}
          />
        ),
      )}
    </div>
  )
}

/** 从第 index 张卡的 iframe srcDoc 提取宿主注入的实例令牌 */
function tokenOf(index: number): string {
  const bodies = screen.getAllByTestId('content-card-webview-body')
  const frame = bodies[index].querySelector('iframe')
  expect(frame).not.toBeNull()
  const srcDoc = frame!.getAttribute('srcdoc') ?? ''
  const token = srcDoc.match(/var TOKEN = "([^"]+)"/)?.[1]
  expect(token).toBeTruthy()
  return token!
}

/** 模拟卡内自报高度（真实校验链路：origin/魔数/令牌全过） */
function postResize(index: number, height: number): void {
  const bodies = screen.getAllByTestId('content-card-webview-body')
  const frame = bodies[index].querySelector('iframe') as HTMLIFrameElement
  act(() => {
    window.dispatchEvent(
      new MessageEvent('message', {
        source: frame.contentWindow,
        origin: 'null',
        data: {
          __agentos_webview: true,
          __wv_token: tokenOf(index),
          method: 'webview.resize',
          params: { height },
        },
      }),
    )
  })
}

describe('ContentCardRenderer — webview 卡全流渲染池', () => {
  it('全流最近 3 张挂载：8 张卡只挂 3 个 iframe，其余容器占位不塌', () => {
    render(cardList(8))
    expect(screen.getAllByTitle('content-card-preview')).toHaveLength(3)
    const bodies = screen.getAllByTestId('content-card-webview-body')
    expect(bodies).toHaveLength(8)
    // 未挂载者容器仍在（高度持久化占位），无 iframe
    expect(bodies.filter((b) => b.querySelector('iframe'))).toHaveLength(3)
  })

  it('空位回收：已挂卡卸载后，队列下一张补位（总量不破限）', () => {
    const view = render(cardList(8))
    expect(screen.getAllByTitle('content-card-preview')).toHaveLength(3)
    act(() => {
      view.rerender(cardList(8, 0)) // 卸载 card-0（已挂）
    })
    expect(screen.getAllByTitle('content-card-preview')).toHaveLength(3)
    // 补位语义：卸载 card-0 后 DOM 重排（bodies[0] = 原 card-1），原排队者
    // card-3 获得挂载——卸载后 DOM 序前 3 张（原 card-1/2/3）全部挂载
    const bodies = screen.getAllByTestId('content-card-webview-body')
    const mountedIndexes = bodies
      .map((b, i) => (b.querySelector('iframe') ? i : -1))
      .filter((i) => i >= 0)
    expect(mountedIndexes).toEqual([0, 1, 2])
  })

  it('距离裁决 + 高度持久化：跌出最近 N 卸 iframe 不塌高度', async () => {
    const view = render(cardList(3))
    // card-0 自报高度 800px（真实校验链路）
    postResize(0, 800)
    const bodies = screen.getAllByTestId('content-card-webview-body')
    await waitFor(() => expect(bodies[0].style.height).toBe('800px'))

    // 距离桩：card-0 被钉到视口上方远处（距离 1800），新注册卡距离 0 更近
    const farRect = {
      top: -2000,
      bottom: -1800,
      left: 0,
      right: 100,
      width: 100,
      height: 200,
      x: 0,
      y: -2000,
      toJSON: () => ({}),
    } as unknown as DOMRect
    vi.spyOn(bodies[0], 'getBoundingClientRect').mockReturnValue(farRect)

    // 再注册 3 张新卡（距离 0 < card-0 的 1800）→ card-0 跌出最近 3
    act(() => {
      view.rerender(cardList(6))
    })

    // card-0 iframe 卸载，但容器高度保持已测值——零布局突变不变量
    await waitFor(() => {
      const fresh = screen.getAllByTestId('content-card-webview-body')
      expect(fresh[0].querySelector('iframe')).toBeNull()
      expect(fresh[0].style.height).toBe('800px')
    })
    expect(screen.getAllByTitle('content-card-preview')).toHaveLength(3)
  })

  it('距离四象限：视口内/下方/上方远处的裁决各归其位', () => {
    const view = render(cardList(4))
    const rectOf = (top: number, bottom: number) =>
      ({
        top,
        bottom,
        left: 0,
        right: 100,
        width: 100,
        height: bottom - top,
        x: 0,
        y: top,
        toJSON: () => ({}),
      }) as unknown as DOMRect
    const stub = (index: number, top: number, bottom: number) => {
      const fresh = screen.getAllByTestId('content-card-webview-body')
      vi.spyOn(fresh[index], 'getBoundingClientRect').mockReturnValue(rectOf(top, bottom))
    }
    // card-0 钉到视口上方远处（最远）；card-1 保持 jsdom 零矩形（bottom<=0 离屏界）；
    // card-2 视口内部（距离 0）；card-3 视口下方（top>=vh 距离为正）
    stub(0, -2000, -1800)
    stub(2, 100, 300)
    stub(3, 2000, 2200)
    act(() => {
      // 注册第 5 张卡触发池重裁（同数量 rerender 不产生新注册）
      view.rerender(cardList(5))
    })
    const bodies = screen.getAllByTestId('content-card-webview-body')
    const mounted = bodies.map((b, i) => (b.querySelector('iframe') ? i : -1)).filter((i) => i >= 0)
    // 最近 3 = card-1(0)/card-2(0)/card-4(0)；card-3(1232) 与最远的 card-0(1800) 卸载
    expect(mounted).toEqual([1, 2, 4])
    expect(bodies[0].querySelector('iframe')).toBeNull()
    expect(bodies[3].querySelector('iframe')).toBeNull()
  })

  it('滚动触发池重裁：捕获期 scroll 经 rAF 节流后按新距离裁决', async () => {
    render(cardList(4))
    const rectOf = (top: number, bottom: number) =>
      ({
        top,
        bottom,
        left: 0,
        right: 100,
        width: 100,
        height: bottom - top,
        x: 0,
        y: top,
        toJSON: () => ({}),
      }) as unknown as DOMRect
    const stub = (index: number, top: number, bottom: number) => {
      const fresh = screen.getAllByTestId('content-card-webview-body')
      vi.spyOn(fresh[index], 'getBoundingClientRect').mockReturnValue(rectOf(top, bottom))
    }
    stub(0, -2000, -1800)
    stub(2, 100, 300)
    stub(3, 2000, 2200)
    act(() => {
      document.dispatchEvent(new Event('scroll'))
    })
    await waitFor(() => {
      const fresh = screen.getAllByTestId('content-card-webview-body')
      // 首裁：最近 3 = card-1/card-2/card-4?（只有 4 张）→ card-1(0)/card-2(0)/card-3(1232)
      expect(fresh[0].querySelector('iframe')).toBeNull()
    })

    // 滚动后：card-0 进入视口内（0），card-2/card-3 滚到上方远处
    stub(0, 100, 300)
    stub(2, -2000, -1800)
    stub(3, -1500, -1300)
    act(() => {
      // 连发两次 scroll：第二次命中节流早退（同帧内只排一次 rAF）
      document.dispatchEvent(new Event('scroll'))
      document.dispatchEvent(new Event('scroll'))
    })
    await waitFor(() => {
      const fresh = screen.getAllByTestId('content-card-webview-body')
      // 最近 3 = card-0(0)/card-1(0)/card-3(1300)，card-2(1800) 让位
      expect(fresh[0].querySelector('iframe')).not.toBeNull()
      expect(fresh[2].querySelector('iframe')).toBeNull()
    })
    const bodies = screen.getAllByTestId('content-card-webview-body')
    expect(bodies.map((b) => !!b.querySelector('iframe'))).toEqual([true, true, false, true])
  })
})
