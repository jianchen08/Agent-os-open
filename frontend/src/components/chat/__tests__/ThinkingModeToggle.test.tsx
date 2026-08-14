/**
 * 功能测试：ThinkingModeToggle 思考强度四档选择器
 *
 * 推演链：思考模式分档需求 → 决策「四档强度（关闭/低/中/高）选择器」→ 功能点：
 * - 显示当前档（关闭→「普通模式」outline；低/中/高→「思考·X」高亮）
 * - 下拉菜单四档可选，点击回调 onStrengthChange
 * - 模型无效/禁用时不可交互
 */

import { render, screen, fireEvent, act } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { ThinkingModeToggle } from '../ThinkingModeToggle'
import type { ThinkingStrength } from '@/types/thinkingMode'

function renderToggle(
  props: {
    strength?: ThinkingStrength
    onStrengthChange?: (s: ThinkingStrength) => void
    disabled?: boolean
    currentModel?: string
  } = {},
) {
  return render(
    <ThinkingModeToggle
      currentModel={props.currentModel ?? 'deepseek-v3'}
      strength={props.strength ?? 'medium'}
      onStrengthChange={props.onStrengthChange ?? (() => {})}
      disabled={props.disabled ?? false}
    />,
  )
}

async function openMenu() {
  const trigger = screen.getByTestId('thinking-strength-trigger')
  fireEvent.pointerDown(trigger)
  fireEvent.pointerUp(trigger)
  fireEvent.click(trigger)
  await screen.findByRole('menu')
}

describe('ThinkingModeToggle — 四档强度选择器', () => {
  it('显示当前档：medium → 「思考·中」', () => {
    renderToggle({ strength: 'medium' })
    expect(screen.getByTestId('thinking-strength-trigger')).toHaveTextContent('思考·中')
  })

  it('显示当前档：off → 「普通模式」', () => {
    renderToggle({ strength: 'off' })
    expect(screen.getByTestId('thinking-strength-trigger')).toHaveTextContent('普通模式')
  })

  it('菜单包含四档：关闭/低/中/高，当前档有选中标记', async () => {
    renderToggle({ strength: 'high' })
    await openMenu()

    expect(screen.getByRole('menuitem', { name: /关闭/ })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /低/ })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /中/ })).toBeInTheDocument()
    expect(screen.getByRole('menuitem', { name: /高/ })).toBeInTheDocument()
  })

  it('点击「关闭」回调 onStrengthChange("off")', async () => {
    const onStrengthChange = vi.fn()
    renderToggle({ strength: 'high', onStrengthChange })
    await openMenu()

    await act(async () => {
      fireEvent.click(screen.getByRole('menuitem', { name: /关闭/ }))
    })
    expect(onStrengthChange).toHaveBeenCalledWith('off')
  })

  it('点击「低」回调 onStrengthChange("low")', async () => {
    const onStrengthChange = vi.fn()
    renderToggle({ strength: 'off', onStrengthChange })
    await openMenu()

    await act(async () => {
      fireEvent.click(screen.getByRole('menuitem', { name: /^低/ }))
    })
    expect(onStrengthChange).toHaveBeenCalledWith('low')
  })

  it('disabled 时触发器不可点', () => {
    renderToggle({ disabled: true })
    expect(screen.getByTestId('thinking-strength-trigger')).toBeDisabled()
  })

  it('模型无效时禁用（提示选择有效模型）', () => {
    renderToggle({ currentModel: 'unknown' })
    const trigger = screen.getByTestId('thinking-strength-trigger')
    expect(trigger).toBeDisabled()
    expect(trigger).toHaveAttribute('title', expect.stringContaining('模型无效'))
  })
})
