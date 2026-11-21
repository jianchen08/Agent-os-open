/**
 * 长按说话（按住说话）手势状态机
 *
 * 仅触屏指针（pointerType=touch/pen）生效：空白输入框上按住达到阈值即开始录音；
 * 按住期间上移超过取消距离进入「松开取消」态，松手丢弃；未达阈值松手是普通点击
 * （聚焦/键盘等默认行为不受影响）。鼠标不启用长按（桌面走麦克风按钮点击路径）。
 */

import { useCallback, useEffect, useRef, useState } from 'react'
import type { MouseEvent as ReactMouseEvent, PointerEvent as ReactPointerEvent } from 'react'

/** 长按触发阈值（毫秒）：未达阈值松手按普通点击处理 */
export const HOLD_TO_TALK_DELAY_MS = 350
/** 上移取消阈值（像素）：按住期间手指上滑达到该距离进入「松开取消」态 */
export const HOLD_TO_TALK_CANCEL_DISTANCE_PX = 60
/** 起录/取消的振动反馈时长（毫秒）；无振动能力的环境无副作用 */
export const HOLD_TO_TALK_VIBRATE_MS = 10

/** 长按手势状态：idle 未按住 / pressing 按住未达阈值 / recording 录音中 / cancelling 松开取消 */
export type HoldToTalkState = 'idle' | 'pressing' | 'recording' | 'cancelling'

export interface UseHoldToTalkOptions {
  /** 入口是否可用（语音不支持时关闭） */
  enabled?: boolean
  /** 按下瞬间判定：输入框当前为空（有文字时长按保持文本选择，不拦截默认行为） */
  canStart: () => boolean
  /** 达到阈值：开始录音（调用方负责收键盘等接线动作） */
  onStart: () => void | Promise<void>
  /** 正常松手：停止录音，走转写/附件后续流程 */
  onStop: () => void
  /** 取消（取消态松手 / pointercancel）：丢弃本次录音 */
  onCancel: () => void
}

export interface UseHoldToTalkReturn {
  /** 当前手势状态 */
  state: HoldToTalkState
  /** 按住生效期间需锁定文本选择与系统长按菜单（textarea 样式据此禁用） */
  isSelectionLocked: boolean
  onPointerDown: (event: ReactPointerEvent) => void
  onPointerMove: (event: ReactPointerEvent) => void
  onPointerUp: (event: ReactPointerEvent) => void
  onPointerCancel: (event: ReactPointerEvent) => void
  onContextMenu: (event: ReactMouseEvent) => void
}

export function useHoldToTalk({
  enabled = true,
  canStart,
  onStart,
  onStop,
  onCancel,
}: UseHoldToTalkOptions): UseHoldToTalkReturn {
  const [state, setState] = useState<HoldToTalkState>('idle')
  /** 事件处理器在计时器/连续事件里读最新状态（state 闭包会滞后一拍） */
  const stateRef = useRef<HoldToTalkState>('idle')
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const startYRef = useRef(0)

  const transition = useCallback((next: HoldToTalkState) => {
    stateRef.current = next
    setState(next)
  }, [])

  const clearHoldTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current)
      timerRef.current = null
    }
  }, [])

  /** 起录/取消的触觉反馈（best-effort：无振动能力环境静默） */
  const vibrate = useCallback(() => {
    navigator.vibrate?.(HOLD_TO_TALK_VIBRATE_MS)
  }, [])

  const onPointerDown = useCallback(
    (event: ReactPointerEvent) => {
      // 长按仅服务触屏；鼠标在桌面走按钮点击，不启用长按手势
      if (event.pointerType !== 'touch' && event.pointerType !== 'pen') return
      if (!enabled || stateRef.current !== 'idle') return
      if (!canStart()) return

      startYRef.current = event.clientY
      transition('pressing')
      timerRef.current = setTimeout(() => {
        timerRef.current = null
        if (stateRef.current !== 'pressing') return
        transition('recording')
        vibrate()
        void onStart()
      }, HOLD_TO_TALK_DELAY_MS)
    },
    [enabled, canStart, onStart, transition, vibrate],
  )

  const onPointerMove = useCallback(
    (event: ReactPointerEvent) => {
      if (stateRef.current !== 'recording') return
      if (startYRef.current - event.clientY >= HOLD_TO_TALK_CANCEL_DISTANCE_PX) {
        transition('cancelling')
      }
    },
    [transition],
  )

  const onPointerUp = useCallback(() => {
    if (stateRef.current === 'pressing') {
      // 未达阈值：普通点击，聚焦/键盘等默认行为照常
      clearHoldTimer()
      transition('idle')
      return
    }

    clearHoldTimer()
    if (stateRef.current === 'recording') {
      transition('idle')
      onStop()
      return
    }
    if (stateRef.current === 'cancelling') {
      transition('idle')
      vibrate()
      onCancel()
    }
  }, [clearHoldTimer, transition, onStop, onCancel, vibrate])

  const onPointerCancel = useCallback(() => {
    if (stateRef.current === 'pressing') {
      clearHoldTimer()
      transition('idle')
      return
    }
    if (stateRef.current === 'recording' || stateRef.current === 'cancelling') {
      clearHoldTimer()
      transition('idle')
      vibrate()
      onCancel()
    }
  }, [clearHoldTimer, transition, onCancel, vibrate])

  const onContextMenu = useCallback((event: ReactMouseEvent) => {
    // 长按已生效：抑制系统长按菜单（文本选择由 isSelectionLocked 样式锁定）
    if (stateRef.current === 'recording' || stateRef.current === 'cancelling') {
      event.preventDefault()
    }
  }, [])

  useEffect(() => clearHoldTimer, [clearHoldTimer])

  return {
    state,
    isSelectionLocked: state === 'recording' || state === 'cancelling',
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onPointerCancel,
    onContextMenu,
  }
}
