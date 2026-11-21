/** @feature FP-0.2.五 审批闭环 @vision V2 全能闭环 @ci frontend-test */
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog'

/**
 * 弹窗层级契约：默认 50；调用方可传更高 zIndex 同时作用于内容与遮罩。
 *
 * 背景：审批卡详情窗挂在 GlobalInteractionOverlay（z-[10000]）内部触发，
 * 默认 50 的弹窗会被底部交互层盖住——真机表现为「点选项后背景变暗、弹窗
 * 不可见、卡不消失」。InteractionCard 传 zIndex=10001 修复。
 */
describe('DialogContent zIndex', () => {
  it('默认内容与遮罩 z-index=50（既有行为不变）', () => {
    render(
      <Dialog open>
        <DialogContent>
          <DialogTitle>默认层级</DialogTitle>
        </DialogContent>
      </Dialog>,
    )

    expect(screen.getByRole('dialog')).toHaveStyle({ zIndex: '50' })
    const layers = Array.from(document.querySelectorAll<HTMLElement>('[style*="z-index"]'))
    expect(layers.filter((el) => el.style.zIndex === '50').length).toBeGreaterThanOrEqual(2)
  })

  it('传入 zIndex 时内容与遮罩同层提升（可压过 z-[10000] 交互层）', () => {
    render(
      <Dialog open>
        <DialogContent zIndex={10001}>
          <DialogTitle>高亮层级</DialogTitle>
        </DialogContent>
      </Dialog>,
    )

    expect(screen.getByRole('dialog')).toHaveStyle({ zIndex: '10001' })
    const layers = Array.from(document.querySelectorAll<HTMLElement>('[style*="z-index"]'))
    // 内容 + 遮罩
    expect(layers.filter((el) => el.style.zIndex === '10001').length).toBeGreaterThanOrEqual(2)
  })
})
