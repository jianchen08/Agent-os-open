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
 * 性能边界（写进插件开发文档的契约）：
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
import { Button } from '@/components/ui/button'
import { API_ENDPOINTS } from '@/constants/api'
import { apiClient } from '@/services/api/client'
import { EXT_ROUTE, extUrl } from '@/services/api/extRoute'
import { buildWebviewThemeTokens } from '@/services/webviewThemeTokens'
import { useSessionStore } from '@/stores/sessionStore'
import { getActiveSessionTheme, useSessionThemeStore } from '@/stores/sessionThemeStore'
import { useThemeStore } from '@/stores/themeStore'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import { loggers } from '@/utils/logger'
import { buildWebviewMessage, validateWebviewEvent } from '@/utils/postMessageSecurity'

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
  /**
   * 宿主数据注入（消息卡宿主桥，web/cards/compression.html 输入契约）：
   * 就绪后经下行桥以 method "message.data" 推送 params = { message }——
   * 卡未收到前呈折叠占位态（不伪造计数）。可选；通用 widget 不带此 prop。
   */
  injectMessage?: { content: string; metadata?: Record<string, unknown> | null }
}

/** 注入 iframe 的 bootstrap JS：暴露 window.agentos.postMessage 给插件 HTML。
 *  携带宿主下发的实例级令牌（__wv_token）——上行消息的身份凭据。
 *  同时预置 window.agentos.ctx（挂载时活跃会话）——后续变化靠宿主 ctx.sync 下行推。 */
function bootstrapJs(instanceToken: string, sessionId: string | null): string {
  const token = JSON.stringify(instanceToken)
  const sid = JSON.stringify(sessionId ?? '')
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
  window.agentos = { postMessage: post, ctx: { sessionId: ${sid} } };
  // 通知宿主 webview 已就绪
  post('__ready', {});
})();
</script>`
}

/** 注入 iframe 的 CSP：限制脚本/样式来源，防御 XSS。 */
const CSP_META =
  '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src data:; connect-src \'none\';">'

/** 取数挂起判定窗：超窗即转显式超时错误态（可重试）。请求可能在浏览器连接
 *  队列或内核饱和中静默悬挂——axios timeout 只从网络层发起才计时，组件层是
 *  唯一能兜住该悬挂的层；时长与 API_TIMEOUT 同口径（30s 拿不到页面 HTML
 *  即不让用户干等）。 */
const PENDING_TIMEOUT_MS = 30_000

/**
 * 把原始 HTML 包装成安全 srcDoc：插 CSP meta（head 最前）+ bootstrap JS。
 * 有 head → 两者注入 head 开标签后（bootstrap 先于 body 脚本解析，桥不缺位）；
 * 只有 html → 注入合成 head；无结构 HTML → 包一层（bootstrap 在 body 末）。
 * sessionId 为挂载时活跃会话（bootstrap ctx 预置）。
 */
function wrapHtml(html: string, instanceToken: string, sessionId: string | null): string {
  const injected = `${CSP_META}${bootstrapJs(instanceToken, sessionId)}`
  if (/<head[^>]*>/i.test(html)) {
    return html.replace(/<head[^>]*>/i, (m) => `${m}${injected}`)
  }
  if (/<html[^>]*>/i.test(html)) {
    return html.replace(/<html[^>]*>/i, (m) => `${m}<head>${injected}</head>`)
  }
  // 无结构 HTML：包一层
  return `<!DOCTYPE html><html><head>${CSP_META}</head><body>${html}${bootstrapJs(instanceToken, sessionId)}</body></html>`
}

export function WebviewWidget({
  pluginId,
  htmlPath,
  widgetId,
  title,
  injectMessage,
}: WebviewWidgetProps): React.ReactNode {
  const [html, setHtml] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  /** 取数代次：重试按钮自增，驱动 effect 重新发起请求 */
  const [attempt, setAttempt] = useState(0)
  /** webview 就绪信号（iframe load 或上行 __ready 先到者）：下行桥推送的门闩 */
  const [webviewReady, setWebviewReady] = useState(false)
  const iframeRef = useRef<HTMLIFrameElement>(null)

  // 实例级令牌（安全审查 B-4）：每次挂载生成，注入 iframe bootstrap，
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

  // 下行桥订阅面（面板-宿主融合协议）：会话上下文 + 生效主题
  const activeSessionId = useSessionStore((s) => s.activeSessionId)
  const globalThemeConfig = useThemeStore((s) => s.themeConfig)
  const activePluginTheme = useThemeStore((s) => s.activePluginTheme)
  const sessionStacks = useSessionThemeStore((s) => s.stacks)

  // 生效主题口径（与 useSessionThemeScope 一致）：活跃会话 override 栈顶
  // （面板容器作用域内实际生效者）优先；否则宿主全局主题——base 配置之上
  // 叠插件主题（contributes.themes）声明的变量覆盖。
  const effectiveTheme = useMemo(() => {
    const override = getActiveSessionTheme(activeSessionId)
    return {
      config: override ?? globalThemeConfig,
      pluginVars: override ? null : (activePluginTheme?.variables ?? null),
    }
    // sessionStacks 引用变化（入栈/出栈）即重算；getActiveSessionTheme 读栈顶
  }, [activeSessionId, sessionStacks, globalThemeConfig, activePluginTheme])

  const endpoint = useMemo(() => {
    if (!pluginId) return null
    const path = htmlPath ?? '/webview'
    return extUrl(pluginId, path)
  }, [pluginId, htmlPath])

  // fetch 插件 HTML（带 Bearer token，token 不进 iframe URL）。
  // 状态机契约：终态显式可见——成功落 html；失败/挂起超时落 error（附重试
  // 按钮），绝不静默停留「加载 Webview...」占位。
  useEffect(() => {
    if (!endpoint) {
      setError('WebviewWidget 缺少 pluginId')
      return
    }
    let cancelled = false
    // 本次取数是否已超时落账（超时后 abort 派生的 reject 不得覆盖超时错误态）
    let timedOut = false
    const controller = new AbortController()
    setError(null)
    const timer = window.setTimeout(() => {
      timedOut = true
      controller.abort()
      if (!cancelled) setError('加载超时，请重试')
    }, PENDING_TIMEOUT_MS)
    apiClient
      .get<string>(endpoint, {
        responseType: 'text',
        transformResponse: [(d) => d],
        signal: controller.signal,
      })
      .then((res) => {
        window.clearTimeout(timer)
        if (cancelled || timedOut) return
        setWebviewReady(false) // 新文档即将注入：等重新就绪后再恢复下行桥推送
        setHtml(
          wrapHtml(
            typeof res.data === 'string' ? res.data : String(res.data),
            instanceToken,
            // 挂载时活跃会话（bootstrap ctx 预置；后续变化靠 ctx.sync 推）
            useSessionStore.getState().activeSessionId,
          ),
        )
      })
      .catch((e) => {
        window.clearTimeout(timer)
        if (cancelled || timedOut) return
        // message 可能为空串（空串越过 ?? 兜底会 falsy 回加载态），按真值兜底
        setError((e as Error).message || '加载插件 HTML 失败')
      })
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [endpoint, attempt])

  // 收 iframe 上行消息（校验 origin + 协议 + 实例令牌）→ 按 method 路由到后端
  // handler 在 window 上注册，不依赖 iframeRef 是否就绪（实际 sendDown 用 optional chaining）
  useEffect(() => {
    const handler = async (event: MessageEvent) => {
      const msg = validateWebviewEvent(event, instanceToken)
      if (!msg) return // 不可信消息丢弃（origin/协议/令牌任一不匹配）
      if (msg.method === '__ready') {
        loggers.websocket.info(`[WebviewWidget] ${widgetId ?? '?'} 就绪`)
        setWebviewReady(true)
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
        if (msg.method === 'theme.apply') {
          // 主题桥协议（模式体系 §5.0）：白名单宿主侧方法——载荷为结构化
          // ThemeConfig 档，按收到时的当前会话入 override 栈；schema 校验
          // 失败整包丢弃（零状态变更）。纯宿主行为，不经内核 transport。
          const sessionId = useSessionStore.getState().activeSessionId
          if (!sessionId) {
            sendDown('error', { message: 'theme.apply 已丢弃：宿主当前无活跃会话' })
            return
          }
          const applied = useSessionThemeStore.getState().pushTheme(sessionId, msg.params)
          if (!applied) {
            sendDown('error', { message: 'theme.apply 已丢弃：载荷不是合法的 ThemeConfig 档' })
            return
          }
          sendDown('result', { applied: true })
          return
        }
        if (msg.method.startsWith('/')) {
          // REST 路径约定：以 '/' 开头视为插件自定义 HTTP 端点。
          // 路由白名单（安全审查 B-4）：只允许本插件的 /ext/{pluginId}/ 前缀，
          // 防 iframe 内容（或被注入的第三方帧）借 host 的 Bearer 直呼内核 API / 他插件端点。
          const extPrefix = `${EXT_ROUTE}/${pluginId ?? ''}/`
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
          res = await apiClient.post(API_ENDPOINTS.ACTIONS.EXECUTE, {
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

  // 下行桥 theme.sync（面板-宿主融合协议）：就绪时推当前生效主题的 token map，
  // 主题变化（全局切换/会话出入栈/插件皮肤）时重推；未就绪不推（__ready/load
  // 就绪后会以当时值补推，不丢终态）
  useEffect(() => {
    if (!webviewReady) return
    iframeRef.current?.contentWindow?.postMessage(
      buildWebviewMessage('theme.sync', {
        tokens: buildWebviewThemeTokens(effectiveTheme.config, effectiveTheme.pluginVars),
      }),
      '*', // sandbox iframe origin='null'，同上
    )
  }, [webviewReady, effectiveTheme])

  // 下行桥 ctx.sync：就绪时推当前活跃会话，会话切换时重推（无会话推空串）
  useEffect(() => {
    if (!webviewReady) return
    iframeRef.current?.contentWindow?.postMessage(
      buildWebviewMessage('ctx.sync', { sessionId: activeSessionId ?? '' }),
      '*',
    )
  }, [webviewReady, activeSessionId])

  // 下行桥 message.data（消息卡宿主桥）：就绪时按卡输入契约推送宿主消息数据
  // （content + metadata 含 compression_ref）。挂载即推一次；消息内容是不可变
  // 终态（落库记录），无需随引用变化重推。
  useEffect(() => {
    if (!webviewReady || !injectMessage) return
    iframeRef.current?.contentWindow?.postMessage(
      buildWebviewMessage('message.data', { message: injectMessage }),
      '*', // sandbox iframe origin='null'，同上
    )
  }, [webviewReady, injectMessage])

  if (error) {
    return (
      <div className="text-muted-foreground flex h-full flex-col items-center justify-center gap-3 text-sm">
        <div className="flex items-center">
          <FileWarning className="mr-2 h-5 w-5" />
          Webview 加载失败: {error}
        </div>
        <Button variant="outline" size="sm" onClick={() => setAttempt((n) => n + 1)}>
          重试
        </Button>
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
        onLoad={() => setWebviewReady(true)}
        className="absolute inset-0 border-0 bg-[var(--web-canvas)]"
        style={{ width: '100%', height: '100%' }}
      />
    </div>
  )
}

export default WebviewWidget
