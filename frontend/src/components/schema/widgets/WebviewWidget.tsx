/**
 * WebviewWidget — VS Code 风格的插件自由 UI 沙箱（ADR §3.4'）
 *
 * 安全模型：
 * - iframe 用 srcDoc 注入（前端 fetch HTML 带 Bearer → srcDoc），**不开 allow-same-origin**
 *   → iframe 是独立 opaque origin，插件 JS 无法访问宿主 cookie/token。
 * - sandbox = allow-scripts allow-forms allow-popups allow-modals（去掉 allow-same-origin）。
 * - postMessage 双向通信：iframe 内 JS → parent.postMessage → 宿主校验 origin + 协议魔数
 *   → 转发到 globalWS（带 token）。下行：widgetEventStore.latest → iframe.postMessage。
 * - 注入 CSP meta + bootstrap JS（暴露 window.agentos.postMessage）。
 *
 * 与 HtmlPreviewWidget 的区别：HtmlPreviewWidget 是受信内容预览（开 allow-same-origin），
 * WebviewWidget 是**不可信插件代码**执行沙箱（绝不开 allow-same-origin）。
 *
 * 性能边界（实测，写进插件开发文档的契约）：
 * - 创建 50-200ms（新 browsing context，每实例付一次）；单次通信 0.5-2ms
 *   （postMessage 序列化）；每实例 ~1-5MB 独立 JS 堆。
 * - 适合：低频交互（按钮/表单）、中频更新（进度条/状态刷新）、整页内容（编辑器/画板）。
 * - 吃力：60fps 高频实时同步、几十个实例并发、MB 级数据流。
 * 插件侧避免在 iframe 内做高频实时渲染，需要时让插件走预置 Widget（同进程直通）。
 *
 * 作为插件自定义 widget：contributes.widgets 声明 `"widget": "webview"`，
 * props 传 { pluginId, htmlPath, widgetId } 即可注册（RenderingEngine 原样透传 props）。
 */
import React, { useEffect, useMemo, useRef, useState } from 'react'
import { FileWarning } from '@/assets/icons'
import { apiClient } from '@/services/api/client'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import { buildWebviewMessage, validateWebviewEvent } from '@/utils/postMessageSecurity'
import { loggers } from '@/utils/logger'

/** Webview widget 渲染指令 props（由 RenderingEngine 从 contributes.widgets 注入） */
export interface WebviewWidgetProps {
  /** 插件 id（用于拼 /ext/{pluginId}/webview 端点） */
  pluginId?: string
  /** 插件提供的 HTML 资源路径（相对插件根，如 "webview/index.html"）；缺省取 "/webview" */
  htmlPath?: string
  /** widget 实例 id（订阅 widgetEventStore.latest 用） */
  widgetId?: string
  /** 标题 */
  title?: string
}

/** 注入 iframe 的 bootstrap JS：暴露 window.agentos.postMessage 给插件 HTML。
 *  携带宿主下发的实例级令牌（__wv_token）——上行消息的身份凭据。 */
function bootstrapJs(instanceToken: string): string {
  const token = JSON.stringify(instanceToken)
  return `<script>
(function(){
  var seq = 0;
  var TOKEN = ${token};
  function post(method, params){
    var id = 'wv_' + (++seq) + '_' + Date.now();
    var msg = { __agentos_webview: true, __wv_token: TOKEN, id: id, method: method };
    if (params !== undefined) msg.params = params;
    parent.postMessage(msg, '*');
    return id;
  }
  window.agentos = { postMessage: post };
  // 通知宿主 webview 已就绪
  post('__ready', {});
})();
</script>`
}

/** 注入 iframe 的 CSP：限制脚本/样式来源，防御 XSS。 */
const CSP_META =
  '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src data:; connect-src \'none\';">'

/**
 * 把原始 HTML 包装成安全 srcDoc：插 CSP meta（head 最前）+ bootstrap JS（body 末尾）。
 * 无 head/body 时简单拼接。
 */
function wrapHtml(html: string, instanceToken: string): string {
  if (/<head[^>]*>/i.test(html)) {
    return html.replace(/<head[^>]*>/i, (m) => `${m}${CSP_META}`)
  }
  if (/<html[^>]*>/i.test(html)) {
    const injected = `${CSP_META}${bootstrapJs(instanceToken)}`
    return html.replace(/<html[^>]*>/i, (m) => `${m}<head>${injected}</head>`)
  }
  // 无结构 HTML：包一层
  return `<!DOCTYPE html><html><head>${CSP_META}</head><body>${html}${bootstrapJs(instanceToken)}</body></html>`
}

export function WebviewWidget({
  pluginId,
  htmlPath,
  widgetId,
  title,
}: WebviewWidgetProps): React.ReactNode {
  const [html, setHtml] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const iframeRef = useRef<HTMLIFrameElement>(null)

  // 实例级令牌（安全审查 2026-08-20 B-4）：每次挂载生成，注入 iframe bootstrap，
  // 上行消息必须携带 — 封死"任意 null-origin 页面伪造消息调用宿主 REST"面
  const instanceTokenRef = useRef<string | null>(null)
  if (instanceTokenRef.current === null) {
    instanceTokenRef.current =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `wv_${Math.random().toString(36).slice(2)}_${Date.now().toString(36)}`
  }
  const instanceToken = instanceTokenRef.current

  // 订阅该 widget 的下行事件
  const latest = useWidgetEventStore((s) => (widgetId ? s.latest[widgetId] : undefined))

  const endpoint = useMemo(() => {
    if (!pluginId) return null
    const path = htmlPath ?? '/webview'
    return `/ext/${pluginId}${path.startsWith('/') ? '' : '/'}${path}`
  }, [pluginId, htmlPath])

  // fetch 插件 HTML（带 Bearer token，token 不进 iframe URL）
  useEffect(() => {
    if (!endpoint) {
      setError('WebviewWidget 缺少 pluginId')
      return
    }
    let cancelled = false
    apiClient
      .get<string>(endpoint, { responseType: 'text', transformResponse: [(d) => d] })
      .then((res) => {
        if (cancelled) return
        setHtml(wrapHtml(typeof res.data === 'string' ? res.data : String(res.data), instanceToken))
      })
      .catch((e) => {
        if (cancelled) return
        setError((e as Error).message ?? '加载插件 HTML 失败')
      })
    return () => {
      cancelled = true
    }
  }, [endpoint])

  // 收 iframe 上行消息（校验 origin + 协议 + 实例令牌）→ 按 method 路由到后端
  // handler 在 window 上注册，不依赖 iframeRef 是否就绪（实际 sendDown 用 optional chaining）
  useEffect(() => {
    const handler = async (event: MessageEvent) => {
      const msg = validateWebviewEvent(event, instanceToken)
      if (!msg) return // 不可信消息丢弃（origin/协议/令牌任一不匹配）
      if (msg.method === '__ready') {
        loggers.websocket.info(`[WebviewWidget] ${widgetId ?? '?'} 就绪`)
        return
      }
      loggers.websocket.debug(`[WebviewWidget] 上行 ${msg.method}`, msg.params)

      // 下行 helper：把结果/错误推回 iframe（origin 用 '*' 因 sandbox iframe origin='null'）
      const sendDown = (suffix: 'result' | 'error', params: unknown): void => {
        iframeRef.current?.contentWindow?.postMessage(
          buildWebviewMessage(`${msg.method}.${suffix}`, params, msg.id),
          '*',
        )
      }

      try {
        let res: unknown
        if (msg.method.startsWith('/')) {
          // REST 路径约定：以 '/' 开头视为插件自定义 HTTP 端点。
          // 路由白名单（安全审查 2026-08-20 B-4）：只允许本插件的 /ext/{pluginId}/ 前缀，
          // 防 iframe 内容（或被注入的第三方帧）借 host 的 Bearer 直呼内核 API / 他插件端点。
          const extPrefix = `/ext/${pluginId ?? ''}/`
          if (!msg.method.startsWith(extPrefix)) {
            sendDown('error', { message: `method 不在本插件路由白名单: ${msg.method}` })
            return
          }
          // 有 params → POST（写操作）；无 params → GET（读操作）
          res =
            msg.params !== undefined
              ? await apiClient.post(msg.method, msg.params)
              : await apiClient.get(msg.method)
        } else {
          // action 约定：复用 command transport 同一端点（带 Bearer token）
          res = await apiClient.post('/api/v1/actions/execute', {
            action: msg.method,
            args: msg.params,
          })
        }
        // axios 响应体在 .data；mock/直返兜底用 res 本身
        const result = (res as { data?: unknown } | undefined)?.data ?? res
        sendDown('result', result)
      } catch (e) {
        const message = (e as Error)?.message ?? String(e)
        loggers.websocket.warn(`[WebviewWidget] 上行 ${msg.method} 失败: ${message}`)
        sendDown('error', { message })
      }
    }
    window.addEventListener('message', handler)
    return () => window.removeEventListener('message', handler)
  }, [widgetId, pluginId, instanceToken])

  // 下行：latest 变化时推给 iframe
  useEffect(() => {
    if (!latest || !iframeRef.current?.contentWindow) return
    const msg = buildWebviewMessage('widget.event', latest)
    // origin 用 '*' 是因为 sandbox iframe origin 是 'null'，需显式指定
    iframeRef.current.contentWindow.postMessage(msg, '*')
  }, [latest])

  if (error) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
        <FileWarning className="mr-2 h-5 w-5" />
        Webview 加载失败: {error}
      </div>
    )
  }

  if (!html) {
    return (
      <div className="text-muted-foreground flex h-full items-center justify-center text-sm">
        加载 Webview...
      </div>
    )
  }

  return (
    <div className="relative h-full w-full">
      <iframe
        ref={iframeRef}
        srcDoc={html}
        title={title ?? 'Webview'}
        // 关键安全：不开 allow-same-origin → iframe 独立 opaque origin，无法访问宿主 token
        sandbox="allow-scripts allow-forms allow-popups allow-modals"
        className="absolute inset-0 border-0 bg-white"
        style={{ width: '100%', height: '100%' }}
      />
    </div>
  )
}

export default WebviewWidget
