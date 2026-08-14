/**
 * DigitalHumanWidget —— 数字人/形象「加载点」占位组件
 *
 * 设计依据:架构 ADR §2.1 / §7.6(`docs/working/重要设计/前端能力统一架构.md`)
 * - 形象(3D/2D/Live2D/VRM)是 **workspace 的 widget**,不是独立空间。
 * - 现阶段只做占位:**不引入** three.js/pixi/live2d 等渲染库。
 *   真实渲染由 0.7.0 路线以插件接入现成引擎(Live2D/TTS/VRM),后端走
 *   Connector 框架(`plugins/shared/system/connectors/`)。
 *
 * 「插件接入就生效」形态:
 * - 现在:显示 source/connector 信息 + 「形象渲染待插件接入」提示。
 * - 将来:形象插件可提供 webview UI,本组件作为 fallback 或 wrapper
 *   (插件未加载 / 未声明 connector 时回落到本占位)。
 *
 * 事件订阅预留:
 * - 若传入 widgetId,订阅 widgetEventStore 的 latest 事件并回显,
 *   为将来表情(expression)/动作(motion)/唇形(lipsync)事件推送预留可观察点。
 *
 * @module DigitalHumanWidget
 */

import React from 'react'
import { useWidgetEventStore } from '@/stores/widgetEventStore'

/** DigitalHumanWidget 属性 */
export interface DigitalHumanWidgetProps {
  /** 形象资源 URI（如 `live2d://model/xxx`、`vrm://models/xxx` 或 https URL） */
  source?: string
  /**
   * 形象资源 URI 别名：前向兼容 ADR §7.6 的 props 命名（`modelUri`）。
   * source 优先，缺省时回退到 modelUri。
   */
  modelUri?: string
  /**
   * 后端形象 Connector 插件名（如 `avatar-live2d-connector`）。
   * 前端先不调用，仅显示信息；将来由插件运行时按此名接入渲染器。
   */
  connector?: string
  /** Widget 实例 id，用于订阅 widgetEventStore 的事件推送（表情/动作等） */
  widgetId?: string
  /** 其余透传 props（widget 注册表统一签名） */
  [key: string]: unknown
}

/**
 * 数字人形象占位组件
 *
 * @param props - 组件属性
 * @returns 占位渲染结果
 */
export function DigitalHumanWidget(props: DigitalHumanWidgetProps): React.ReactNode {
  const { source, modelUri, connector, widgetId } = props
  // source 优先，modelUri 作为别名兜底（ADR §7.6 命名兼容）
  const resolvedSource = source ?? modelUri

  // 订阅 widgetEventStore：仅在有 widgetId 时读取 latest，为将来事件推送预留。
  // 未传 widgetId 时传 undefined，store selector 返回 undefined，不订阅具体实例。
  const latestEvent = useWidgetEventStore((s) =>
    widgetId ? s.latest[widgetId] : undefined,
  )

  return (
    <div
      className="flex h-full w-full flex-col items-center justify-center gap-3 rounded-lg border border-dashed bg-background p-6 text-center"
      data-widget-type="digital_human"
      data-widget-id={widgetId}
    >
      {/* 形象占位图标 */}
      <div className="bg-primary/10 text-primary flex h-12 w-12 items-center justify-center rounded-full">
        <svg
          className="h-6 w-6"
          fill="none"
          viewBox="0 0 24 24"
          stroke="currentColor"
          aria-hidden="true"
        >
          <path
            strokeLinecap="round"
            strokeLinejoin="round"
            strokeWidth={2}
            d="M12 14a4 4 0 10-4-4 4 4 0 004 4zm0 2c-4 0-8 2-8 5v1h16v-1c0-3-4-5-8-5z"
          />
        </svg>
      </div>

      <div className="flex flex-col items-center gap-1">
        <p className="text-foreground text-sm font-medium">形象加载点</p>
        <p className="text-muted-foreground text-xs">数字人 / 3D / 2D 形象</p>
      </div>

      {/* 形象资源信息 */}
      {resolvedSource && (
        <div className="max-w-full overflow-hidden">
          <p className="text-muted-foreground truncate text-xs" title={resolvedSource}>
            {resolvedSource}
          </p>
        </div>
      )}

      {/* 形象 Connector 信息 */}
      {connector && (
        <p className="text-muted-foreground text-xs">
          Connector: <span className="font-mono">{connector}</span>
        </p>
      )}

      {/* 「待插件接入」提示 */}
      <p className="text-muted-foreground text-xs">
        形象渲染待插件接入（Live2D / VRM / TTS）
      </p>

      {/* 事件订阅预留：回显最新事件（表情/动作等），为 0.7.0 推送链路预留 */}
      {latestEvent && (
        <div className="mt-1 w-full max-w-md rounded border bg-muted/30 p-2 text-left">
          <p className="text-muted-foreground text-xs">
            最新事件：
            <span className="font-mono text-foreground">
              {latestEvent.event ?? '(unnamed)'}
            </span>
          </p>
          {latestEvent.data && Object.keys(latestEvent.data).length > 0 && (
            <pre className="mt-1 overflow-x-auto text-[10px] leading-tight text-foreground/80">
              {JSON.stringify(latestEvent.data)}
            </pre>
          )}
        </div>
      )}
    </div>
  )
}

DigitalHumanWidget.displayName = 'DigitalHumanWidget'

export default DigitalHumanWidget
