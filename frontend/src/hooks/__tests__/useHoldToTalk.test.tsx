/** @feature FP-T12 前端适配 @vision V6 可即用 @ci frontend-test */
/**
 * useHoldToTalk 行为契约测试。
 *
 * 覆盖：长按阈值（350ms）触发、短按不触发、有文字/鼠标/禁用不触发、触屏与触控笔
 * 生效、上滑取消阈值（60px）、松手/pointercancel 的取消语义、系统长按菜单抑制与
 * 选择锁定、卸载清计时器。时序全部走 fake timers。
 */
import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  HOLD_TO_TALK_CANCEL_DISTANCE_PX,
  HOLD_TO_TALK_DELAY_MS,
  useHoldToTalk,
} from '../useHoldToTalk'
import type { MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent } from 'react'

/** 指针事件替身：处理器只读 pointerType / clientY 两个字段 */
const pointer = (pointerType: string, clientY = 400) =>
  ({ pointerType, clientY }) as unknown as ReactPointerEvent

/** 挂载手势 hook，返回结果与三个回调桩 */
function renderHold(canStart: () => boolean = () => true, enabled = true) {
  const onStart = vi.fn()
  const onStop = vi.fn()
  const onCancel = vi.fn()
  const rendered = renderHook(() =>
    useHoldToTalk({ enabled, canStart, onStart, onStop, onCancel }),
  )
  return { ...rendered, onStart, onStop, onCancel }
}

/** 振动能力桩：jsdom 无 navigator.vibrate，best-effort 调用应为可选链无副作用 */
function installVibrateStub() {
  Object.defineProperty(navigator, 'vibrate', { value: vi.fn(), configurable: true })
}

function removeVibrateStub() {
  Reflect.deleteProperty(navigator, 'vibrate')
}

describe('useHoldToTalk: 长按触发阈值', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    installVibrateStub()
  })
  afterEach(() => {
    vi.useRealTimers()
    removeVibrateStub()
  })

  it('触屏按住达到 350ms 才起录：349ms 不触发，第 350ms 进入 recording', () => {
    const { result, onStart } = renderHold()
    act(() => result.current.onPointerDown(pointer('touch')))
    expect(result.current.state).toBe('pressing')
    expect(result.current.isSelectionLocked).toBe(false)

    act(() => vi.advanceTimersByTime(HOLD_TO_TALK_DELAY_MS - 1))
    expect(onStart).not.toHaveBeenCalled()

    act(() => vi.advanceTimersByTime(1))
    expect(onStart).toHaveBeenCalledTimes(1)
    expect(result.current.state).toBe('recording')
    expect(result.current.isSelectionLocked).toBe(true)
  })

  it('触控笔（pen）同样生效', () => {
    const { result, onStart } = renderHold()
    act(() => result.current.onPointerDown(pointer('pen')))
    act(() => vi.advanceTimersByTime(HOLD_TO_TALK_DELAY_MS))
    expect(onStart).toHaveBeenCalledTimes(1)
    expect(result.current.state).toBe('recording')
  })

  it('鼠标（mouse）不启用长按：按住再久也不起录', () => {
    const { result, onStart } = renderHold()
    act(() => result.current.onPointerDown(pointer('mouse')))
    act(() => vi.advanceTimersByTime(HOLD_TO_TALK_DELAY_MS * 3))
    expect(onStart).not.toHaveBeenCalled()
    expect(result.current.state).toBe('idle')
  })

  it('输入框有文字（canStart=false）不触发，也不进入按压态', () => {
    const { result, onStart } = renderHold(() => false)
    act(() => result.current.onPointerDown(pointer('touch')))
    act(() => vi.advanceTimersByTime(HOLD_TO_TALK_DELAY_MS * 2))
    expect(onStart).not.toHaveBeenCalled()
    expect(result.current.state).toBe('idle')
  })

  it('入口禁用（enabled=false）不触发', () => {
    const { result, onStart } = renderHold(() => true, false)
    act(() => result.current.onPointerDown(pointer('touch')))
    act(() => vi.advanceTimersByTime(HOLD_TO_TALK_DELAY_MS))
    expect(onStart).not.toHaveBeenCalled()
    expect(result.current.state).toBe('idle')
  })

  it('未达阈值松手 = 普通点击：不起录、不停止', () => {
    const { result, onStart, onStop, onCancel } = renderHold()
    act(() => result.current.onPointerDown(pointer('touch')))
    act(() => vi.advanceTimersByTime(HOLD_TO_TALK_DELAY_MS - 100))
    act(() => result.current.onPointerUp(pointer('touch')))
    act(() => vi.advanceTimersByTime(HOLD_TO_TALK_DELAY_MS * 2))
    expect(onStart).not.toHaveBeenCalled()
    expect(onStop).not.toHaveBeenCalled()
    expect(onCancel).not.toHaveBeenCalled()
    expect(result.current.state).toBe('idle')
  })

  it('卸载清掉按住计时器：卸载后不再起录', () => {
    const { result, onStart, unmount } = renderHold()
    act(() => result.current.onPointerDown(pointer('touch')))
    unmount()
    act(() => vi.advanceTimersByTime(HOLD_TO_TALK_DELAY_MS * 2))
    expect(onStart).not.toHaveBeenCalled()
  })
})

describe('useHoldToTalk: 上滑取消与松手语义', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    installVibrateStub()
  })
  afterEach(() => {
    vi.useRealTimers()
    removeVibrateStub()
  })

  /** 按住至录音态（从 clientY=400 起） */
  function holdToRecording(context: ReturnType<typeof renderHold>) {
    act(() => context.result.current.onPointerDown(pointer('touch', 400)))
    act(() => vi.advanceTimersByTime(HOLD_TO_TALK_DELAY_MS))
    expect(context.result.current.state).toBe('recording')
  }

  it('上滑达到 60px 进入「松开取消」态：松手取消，不停止转写', () => {
    const context = renderHold()
    holdToRecording(context)
    act(() => context.result.current.onPointerMove(pointer('touch', 400 - HOLD_TO_TALK_CANCEL_DISTANCE_PX)))
    expect(context.result.current.state).toBe('cancelling')

    act(() => context.result.current.onPointerUp(pointer('touch', 400 - HOLD_TO_TALK_CANCEL_DISTANCE_PX)))
    expect(context.onCancel).toHaveBeenCalledTimes(1)
    expect(context.onStop).not.toHaveBeenCalled()
    expect(context.result.current.state).toBe('idle')
  })

  it('上滑 59px 未达取消线：松手走正常停止', () => {
    const context = renderHold()
    holdToRecording(context)
    act(() => context.result.current.onPointerMove(pointer('touch', 400 - (HOLD_TO_TALK_CANCEL_DISTANCE_PX - 1))))
    expect(context.result.current.state).toBe('recording')

    act(() => context.result.current.onPointerUp(pointer('touch', 400 - (HOLD_TO_TALK_CANCEL_DISTANCE_PX - 1))))
    expect(context.onStop).toHaveBeenCalledTimes(1)
    expect(context.onCancel).not.toHaveBeenCalled()
  })

  it('未上滑时松手：正常停止（不取消）', () => {
    const context = renderHold()
    holdToRecording(context)
    act(() => context.result.current.onPointerUp(pointer('touch', 400)))
    expect(context.onStop).toHaveBeenCalledTimes(1)
    expect(context.onCancel).not.toHaveBeenCalled()
  })

  it('录音中 pointercancel 按取消处理', () => {
    const context = renderHold()
    holdToRecording(context)
    act(() => context.result.current.onPointerCancel(pointer('touch', 400)))
    expect(context.onCancel).toHaveBeenCalledTimes(1)
    expect(context.onStop).not.toHaveBeenCalled()
    expect(context.result.current.state).toBe('idle')
  })

  it('按压未达阈值时 pointercancel：静默复位，无回调', () => {
    const context = renderHold()
    act(() => context.result.current.onPointerDown(pointer('touch', 400)))
    act(() => vi.advanceTimersByTime(100))
    act(() => context.result.current.onPointerCancel(pointer('touch', 400)))
    expect(context.onCancel).not.toHaveBeenCalled()
    expect(context.onStart).not.toHaveBeenCalled()
    expect(context.result.current.state).toBe('idle')
  })
})

describe('useHoldToTalk: 系统长按菜单抑制', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    installVibrateStub()
  })
  afterEach(() => {
    vi.useRealTimers()
    removeVibrateStub()
  })

  it('录音/取消态拦截 contextmenu；未按住时放行（正常右键/选择不受影响）', () => {
    const { result } = renderHold()
    const idleEvent = { preventDefault: vi.fn() }
    act(() => result.current.onContextMenu(idleEvent as unknown as ReactMouseEvent))
    expect(idleEvent.preventDefault).not.toHaveBeenCalled()

    act(() => result.current.onPointerDown(pointer('touch', 400)))
    act(() => vi.advanceTimersByTime(HOLD_TO_TALK_DELAY_MS))
    const holdingEvent = { preventDefault: vi.fn() }
    act(() => result.current.onContextMenu(holdingEvent as unknown as ReactMouseEvent))
    expect(holdingEvent.preventDefault).toHaveBeenCalledTimes(1)
  })
})
