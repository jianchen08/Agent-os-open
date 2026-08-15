/**
 * Godot 选中引用状态 Hook——订阅 selectionBridge 状态变化，
 * thread 切换时重新订阅并拉取快照。
 */
import { useEffect, useState } from 'react'
import {
  getGodotSelection,
  initGodotSelection,
  subscribeGodotSelection,
  type GodotSelectionState,
} from '@/services/godot/selectionBridge'

export function useGodotSelection(threadId: string | undefined): GodotSelectionState {
  const [state, setState] = useState<GodotSelectionState>(getGodotSelection)

  useEffect(() => {
    const unsub = subscribeGodotSelection(setState)
    if (threadId) {
      void initGodotSelection(threadId)
    }
    return unsub
  }, [threadId])

  return state
}
