/**
 * Godot 引用 provider——通用引用 provider 注册缝的首个注册件。
 *
 * 数据通道是 pipeline_godot_context 的 selectionBridge（推送+合并，
 * ADR 2026-09-03-godot-reference-merge-only）；本适配器把桥状态翻译为
 * 通用契约：注入面 ReferenceSelection（发送时随消息拼引用块）+ 展示面
 * ReferenceRowState（输入区实时镜像行）。ChatInput / 镜像行经注册缝消费，
 * 不感知 godot。
 */
import {
  clearGodotSelection,
  getGodotSelection,
  godotPreviewUrl,
  initGodotSelection,
  subscribeGodotSelection,
} from '@/services/godot/selectionBridge'
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
  getRow() {
    const sel = getGodotSelection()
    if (sel.items.length === 0) return null
    return {
      label: 'Godot 引用',
      connected: sel.connected,
      chips: sel.items.map((it, i) => ({
        key: `${it.name}-${it.path}-${i}`,
        kind: 'godot-node',
        title: it.name,
        subtitle: `${it.type} @ ${it.path}${it.position ? ' · ' + it.position : ''}`,
        previewUrl: it.preview_kind ? godotPreviewUrl(i, sel.signature) : undefined,
      })),
      clear: async () => {
        await clearGodotSelection()
      },
    }
  },
  subscribeRow(onChange) {
    return subscribeGodotSelection(() => onChange())
  },
  activateRow(threadId) {
    if (threadId) void initGodotSelection(threadId)
  },
})
