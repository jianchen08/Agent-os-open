/**
 * Godot 引用 provider——通用引用 provider 注册缝的首个注册件。
 *
 * 数据通道仍是 pipeline_godot_context 的 selectionBridge（推送+合并，
 * ADR 2026-09-03-godot-reference-merge-only）；本适配器只把桥状态翻译为
 * 通用 ReferenceSelection，ChatInput 经注册缝消费，不感知 godot。
 */
import { clearGodotSelection, getGodotSelection } from '@/services/godot/selectionBridge'
import { registerReferenceProvider } from './referenceProviders'

registerReferenceProvider({
  source: 'godot',
  getSelection() {
    const sel = getGodotSelection()
    if (sel.items.length === 0) return null
    return {
      source: 'godot',
      attrs: { scene: sel.scene?.path ?? '' },
      items: sel.items.map((it) => ({
        name: it.name,
        type: it.type,
        path: it.path,
        extra: it.position ? `position=${it.position}` : undefined,
      })),
    }
  },
  async consume() {
    await clearGodotSelection()
  },
})
