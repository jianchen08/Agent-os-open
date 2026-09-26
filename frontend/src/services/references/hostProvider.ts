/**
 * 宿主引用 provider——通用引用 provider 注册缝的宿主注册件。
 *
 * 数据通道是 pipeline_host_context 的 hostBridge（推送+合并，
 * ADR 2026-09-03-godot-reference-merge-only）；本适配器把桥状态翻译为
 * 通用契约：注入面 ReferenceSelection（发送时随消息拼引用块）+ 展示面
 * ReferenceRowState（输入区实时镜像行）。ChatInput / 镜像行经注册缝消费，
 * 不感知具体宿主。
 *
 * source 是**动态**的：连接者身份（source/display_name）由插件配置指定并
 * 随快照下发（ADR 2026-09-24-host-context-generic-connection），发送时读
 * 当前快照——同一前端零改动适配任意配置的连接者。注册键 'host' 是本注册
 * 件的槽位标识（同键重注册覆盖），非消息块 source。
 */
import {
  clearHostSelection,
  getHostSelection,
  hostPreviewUrl,
  initHostSelection,
  subscribeHostSelection,
} from '@/services/host/hostBridge'
import { registerReferenceProvider } from './referenceProviders'

registerReferenceProvider({
  source: 'host',
  getSelection() {
    const sel = getHostSelection()
    if (sel.items.length === 0) return null
    return {
      source: sel.source || 'host',
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
    await clearHostSelection()
  },
  getRow() {
    const sel = getHostSelection()
    if (sel.items.length === 0) return null
    const source = sel.source || 'host'
    return {
      label: `${sel.display_name || source} 引用`,
      connected: sel.connected,
      chips: sel.items.map((it, i) => ({
        key: `${it.name}-${it.path}-${i}`,
        kind: `${source}-node`,
        title: it.name,
        subtitle: `${it.type} @ ${it.path}${it.position ? ' · ' + it.position : ''}`,
        previewUrl: it.preview_kind ? hostPreviewUrl(i, sel.signature) : undefined,
      })),
      clear: async () => {
        await clearHostSelection()
      },
    }
  },
  subscribeRow(onChange) {
    return subscribeHostSelection(() => onChange())
  },
  activateRow(threadId) {
    if (threadId) void initHostSelection(threadId)
  },
})
