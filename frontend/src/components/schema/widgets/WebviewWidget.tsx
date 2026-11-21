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
import {
  openModeSession,
  parseModeSessionPayload,
  resolvePersonaInjectionKey,
} from '@/services/modeSessionBinder'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { buildWebviewThemeTokens } from '@/services/webviewThemeTokens'
import { useNotificationStore } from '@/stores/notificationStore'
import { usePersonaPossessStore } from '@/stores/personaPossessStore'
import { usePipelineMessageStore } from '@/stores/pipelineMessageStore'
import { useSessionStore } from '@/stores/sessionStore'
import { getActiveSessionTheme, useSessionThemeStore } from '@/stores/sessionThemeStore'
import { useThemeStore } from '@/stores/themeStore'
import { useUIStore } from '@/stores/uiStore'
import { useWidgetEventStore } from '@/stores/widgetEventStore'
import { loggers } from '@/utils/logger'
import { buildWebviewMessage, validateWebviewEvent } from '@/utils/postMessageSecurity'

/** 命令 owner 查找：contributes.commands 归一化条目按 id（或旧 command 扩展
 *  字段）匹配，返回所属插件 id。仅同插件沙箱可调用该命令（WebviewWidget
 *  上行白名单）。 */
function findCommandOwner(commandId: string): string | undefined {
  const command = contributionRegistry
    .getCommands()
    .find((c) => c.id === commandId || (c.command as string | undefined) === commandId)
  return command?.pluginId
}

/** Webview widget 渲染指令 props（由 RenderingEngine 从 contributes.widgets 注入） */
export interface WebviewWidgetProps {
  /** 插件 id（用于拼 /ext/{pluginId}/webview 端点） */
  pluginId?: string
  /** 插件提供的 HTML 资源路径（相对插件根，如 "webview/index.html"）；缺省取 "/webview" */
  htmlPath?: string
  /**
   * 外部站点内嵌（相对插件 ext 根，如 "/api/dsh/official-ui"）：端点返回
   * `{available, url, starting, reason}`，就绪后按 `url` 渲染 iframe。
   *
   * 与 htmlPath 的差别是**渲染模型**：htmlPath 走沙箱 srcDoc（不可信插件页）；
   * 本 prop 指向独立真实站点，需要顶层文档语义——iframe 带 `src`、不加
   * sandbox、不注入 CSP，否则目标站点的同源脚本 / cookie / WebSocket 全部
   * 不可用（DSH 官方 Web 应用即此形态：SPA + 签名 cookie + `/api` 网关）。
   * 声明面来自插件 manifest（可审计），URL 由插件端点下发，前端不拼接。
   */
  externalFramePath?: string
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
  /**
   * 自适应高度（ADR 2026-10-14-message-card-body-form，通用容器能力）：
   * 开启后容器高度由卡内页经上行 method "webview.resize" {height} 驱动
   * （宿主 clamp 至 [WEBVIEW_MIN_HEIGHT, WEBVIEW_MAX_HEIGHT]px）；可选
   * width 同桥回报容器宽（悬浮槽等 shrink-to-fit 容器需实宽，缺省不动宽）；
   * 缺省关闭 = 既有 100% 填充形态零变化。
   */
  autoHeight?: boolean
  /**
   * 页面声明 CSP 扩展（白名单制）：原始形态任意，经 normalizeCspExtension
   * fail-closed 归一（非法整包丢弃）；仅低危展示向 img-src 可扩展，高危向
   * （script/connect/default）恒锁死——声明来源 = 插件自己的 manifest props，
   * 可审计。缺省 = CSP_META 基线零变化。
   */
  csp?: unknown
  /**
   * 透明底声明（悬浮角标等叠加在宿主界面上的形态）：iframe 元素底色改透明，
   * 插件页自带圆角/底色直接透出宿主——否则圆角四角外露 iframe 的 canvas
   * 方形底（用户所见"圆角钮带方框背景"）。缺省 false = 既有 canvas 底零变化。
   */
  transparent?: boolean
}

/**
 * 注入 iframe 的 bootstrap JS：暴露 window.agentos.postMessage 给插件 HTML。
 * 携带宿主下发的实例级令牌（__wv_token）——上行消息的身份凭据。
 * 同时预置 window.agentos.ctx（挂载时活跃会话）——后续变化靠宿主 ctx.sync 下行推。
 * 导出复用：ContentCardRenderer webview 卡体同款桥形态（content_cards 内联
 * 内容沙箱）。
 *
 * scheme（可选，transparent 悬浮件专用）：宿主生效主题的明暗。传值时把
 * iframe 文档的 color-scheme 显式钉到与宿主一致——**宿主深色而 iframe 文档
 * 未声明 color-scheme 时，Chromium 会把文档画布涂成近白实色**，透明底圆角
 * 件四角随即出现方块底（实测：宿主 `color-scheme:dark` + 未声明 → 角部
 * 残留近白画布底）。theme.sync 携 scheme 重推，主题切换即时纠正。
 */
export function bootstrapJs(
  instanceToken: string,
  sessionId: string | null,
  scheme?: 'light' | 'dark',
): string {
  const token = JSON.stringify(instanceToken)
  const sid = JSON.stringify(sessionId ?? '')
  const initialScheme = JSON.stringify(scheme ?? null)
  return `<script>
(function(){
  var seq = 0;
  var TOKEN = ${token};
  var SCHEME = ${initialScheme};
  function post(method, params){
    var id = 'wv_' + (++seq) + '_' + Date.now();
    var msg = { __agentos_webview: true, __wv_token: TOKEN, id: id, method: method };
    if (params !== undefined) msg.params = params;
    parent.postMessage(msg, '*');
    return id;
  }
  function applyScheme(s){
    if (s !== 'light' && s !== 'dark') return;
    try { document.documentElement.style.colorScheme = s; } catch (e) {}
  }
  if (SCHEME) applyScheme(SCHEME);
  window.addEventListener('message', function(ev){
    var d = ev.data;
    if (!d || d.__agentos_webview !== true || d.method !== 'theme.sync') return;
    applyScheme(d.params && d.params.scheme);
  });
  window.agentos = { postMessage: post, ctx: { sessionId: ${sid} } };
  // 通知宿主 webview 已就绪
  post('__ready', {});
})();
</script>`
}

/** 注入 iframe 的 CSP：限制脚本/样式来源，防御 XSS。（content_cards webview 形态
 *  卡消费同款 CSP——不可信内容沙箱预览的安全形态全仓单源） */
export const CSP_META =
  '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src data:; connect-src \'none\';">'

/**
 * 页面声明可扩展的 CSP 面（白名单制，fail-closed）。
 *
 * 沙箱基座三类高危向（default-src / script-src / connect-src）**不开放扩展**——
 * 脚本执行与外联通道是 webview 沙箱的存在意义；开放的是低危展示向 img-src：
 * 图片加载不执行代码，声明落在插件自己的 manifest（contributes.pages[].props.csp），
 * 可审计、随声明走。典型场景：插件页直连本机服务投屏（browser_tool 直播页签
 * img → Bridge MJPEG，ADR 2026-10-21-browser-live-workspace-tab）。
 */
export interface WebviewCspExtension {
  /** 追加放行的图片来源（origin 形态，如 "http://127.0.0.1:*"；normalizeCspExtension 校验后的产物形态） */
  imgSrc?: string[]
}

/** 合法 img-src 源：http/https + 必有 host + 可选端口（数字或 `*` 通配）。 */
const CSP_SOURCE_RE = /^https?:\/\/[A-Za-z0-9.-]+(?::(\d+|\*))?$/

/**
 * 归一化页面声明的 CSP 扩展：非法条目逐条丢弃（坏声明不崩渲染，renderIntent
 * 同款 fail-closed 口径）；非数组 imgSrc 整字段丢弃。归一后按 CSP 语义去重。
 */
export function normalizeCspExtension(raw: unknown): WebviewCspExtension | null {
  if (!raw || typeof raw !== 'object') return null
  const imgSrc = (raw as Record<string, unknown>).imgSrc
  if (!Array.isArray(imgSrc)) return null
  const sources = new Set<string>()
  for (const entry of imgSrc) {
    if (typeof entry === 'string') {
      const trimmed = entry.trim()
      if (trimmed && CSP_SOURCE_RE.test(trimmed)) sources.add(trimmed)
    }
  }
  return sources.size > 0 ? { imgSrc: [...sources] } : null
}

/**
 * 由默认基线 + 可选扩展合成 CSP meta。扩展只落 img-src（normalizeCspExtension
 * 已过滤形态），基线三类高危向恒锁死；无扩展时与 CSP_META 逐字节一致。
 */
export function buildCspMeta(ext?: WebviewCspExtension | null): string {
  const imgSources = ext?.imgSrc?.length ? ` ${ext.imgSrc.join(' ')}` : ''
  return (
    `<meta http-equiv="Content-Security-Policy" content="default-src 'none'; ` +
    `script-src 'unsafe-inline'; style-src 'unsafe-inline'; ` +
    `img-src data:${imgSources}; connect-src 'none';">`
  )
}

/** 自适应高度夹取界（webview.resize 上行，autoHeight 实例专用） */
export const WEBVIEW_MIN_HEIGHT = 24
export const WEBVIEW_MAX_HEIGHT = 4000

function clampWebviewHeight(raw: unknown): number | null {
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return null
  return Math.min(WEBVIEW_MAX_HEIGHT, Math.max(WEBVIEW_MIN_HEIGHT, Math.round(raw)))
}

/** 自适应宽度夹取界（webview.resize 上行可选 width，autoHeight 实例专用；
 *  0 为合法值 = 卡内容自隐藏态） */
export const WEBVIEW_MAX_WIDTH = 4000

function clampWebviewWidth(raw: unknown): number | null {
  if (typeof raw !== 'number' || !Number.isFinite(raw)) return null
  return Math.min(WEBVIEW_MAX_WIDTH, Math.max(0, Math.round(raw)))
}

/** 取数挂起判定窗：超窗即转显式超时错误态（可重试）。请求可能在浏览器连接
 *  队列或内核饱和中静默悬挂——axios timeout 只从网络层发起才计时，组件层是
 *  唯一能兜住该悬挂的层；时长与 API_TIMEOUT 同口径（30s 拿不到页面 HTML
 *  即不让用户干等）。 */
const PENDING_TIMEOUT_MS = 30_000

/** 外部帧就绪轮询间隔：外部应用冷启动（首次装依赖可达数分钟）是常态，
 *  2s 一探既能及时接上又不会把内核刷爆。 */
const EXTERNAL_FRAME_POLL_MS = 2_000

/** 外部帧就绪等待上限：与插件侧启动超时同量级，超窗落终态并展示对方 reason。 */
const EXTERNAL_FRAME_TIMEOUT_MS = 180_000

/** 外部帧端点契约（插件 http_endpoints 声明面）。 */
export interface ExternalFrameState {
  available: boolean
  url?: string | null
  starting?: boolean
  reason?: string | null
}

/** 只接受 http(s) 绝对 URL：非法形态按错误处理，不把任意 scheme 塞进 iframe.src。 */
export function normalizeExternalFrameUrl(raw: unknown): string | null {
  if (typeof raw !== 'string' || raw === '') return null
  try {
    const parsed = new URL(raw)
    return parsed.protocol === 'http:' || parsed.protocol === 'https:' ? parsed.href : null
  } catch {
    return null
  }
}

/**
 * 把原始 HTML 包装成安全 srcDoc：插 CSP meta（head 最前）+ bootstrap JS。
 * 有 head → 两者注入 head 开标签后（bootstrap 先于 body 脚本解析，桥不缺位）；
 * 只有 html → 注入合成 head；无结构 HTML → 包一层（bootstrap 在 body 末）。
 * sessionId 为挂载时活跃会话（bootstrap ctx 预置）。
 * cspExt：页面声明 CSP 扩展（img-src 白名单；fail-closed 归一后合成）。
 * scheme：文档色彩方案（transparent 悬浮件专用，见 bootstrapJs）——非空时
 * 同时注入 `<meta name="color-scheme">`（首帧即生效，不等 bootstrap 执行）。
 */
function wrapHtml(
  html: string,
  instanceToken: string,
  sessionId: string | null,
  cspExt?: WebviewCspExtension | null,
  scheme?: 'light' | 'dark',
): string {
  const schemeMeta = scheme ? `<meta name="color-scheme" content="${scheme}">` : ''
  const injected = `${buildCspMeta(cspExt)}${schemeMeta}${bootstrapJs(instanceToken, sessionId, scheme)}`
  if (/<head[^>]*>/i.test(html)) {
    return html.replace(/<head[^>]*>/i, (m) => `${m}${injected}`)
  }
  if (/<html[^>]*>/i.test(html)) {
    return html.replace(/<html[^>]*>/i, (m) => `${m}<head>${injected}</head>`)
  }
  // 无结构 HTML：包一层
  return `<!DOCTYPE html><html><head>${buildCspMeta(cspExt)}${schemeMeta}</head><body>${html}${bootstrapJs(instanceToken, sessionId, scheme)}</body></html>`
}

export function WebviewWidget({
  pluginId,
  htmlPath,
  externalFramePath,
  widgetId,
  title,
  injectMessage,
  autoHeight = false,
  csp,
  transparent = false,
}: WebviewWidgetProps): React.ReactNode {
  const [html, setHtml] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  /** 取数代次：重试按钮自增，驱动 effect 重新发起请求 */
  const [attempt, setAttempt] = useState(0)
  /** 自动重试：插件路由注册窗口期（内核（重）启动后插件 http 路由注册晚于面板
   *  加载）的 404/route not found 属瞬态——退避自动重试至多 4 次后再停留终态；
   *  其余错误不自动重试（终态 + 手动重试）。调度就地于取数 catch（依赖 error
   *  状态链的 effect 会被重试加载态的 cleanup 取消断链），计数走 ref */
  const autoRetriesRef = useRef(0)
  /** 瞬态取数中（插件路由注册窗口期）：停留加载语义不落错误态 */
  const [transientPending, setTransientPending] = useState(false)
  /** 组件存活 ref：调度回调不得被上一轮取数 effect 的 cancelled 旗标挡杀 */
  const mountedRef = useRef(true)
  useEffect(() => () => { mountedRef.current = false }, [])
  /** webview 就绪信号（iframe load 或上行 __ready 先到者）：下行桥推送的门闩 */
  const [webviewReady, setWebviewReady] = useState(false)
  /** 自适应高度（autoHeight 实例；webview.resize 上行驱动，缺省下界起步） */
  const [autoHeightPx, setAutoHeightPx] = useState(WEBVIEW_MIN_HEIGHT)
  /** 自适应宽度（autoHeight 实例可选位；webview.resize 上行 width 驱动，
   *  null = 未回报保持 w-full——悬浮槽等 shrink-to-fit 容器需内容自报实宽） */
  const [autoWidthPx, setAutoWidthPx] = useState<number | null>(null)
  const iframeRef = useRef<HTMLIFrameElement>(null)
  /** 外部帧就绪地址（externalFramePath 形态；null = 尚未就绪） */
  const [externalFrameUrl, setExternalFrameUrl] = useState<string | null>(null)

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
  const activePipelineId = usePipelineMessageStore((s) => s.activePipelineId)
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
    // 外部帧形态不取插件 HTML（渲染面是独立站点，见 externalFramePath）
    if (!pluginId || externalFramePath) return null
    const path = htmlPath ?? '/webview'
    return extUrl(pluginId, path)
  }, [pluginId, htmlPath, externalFramePath])

  /** 生效主题明暗（transparent 悬浮件的 iframe 文档 color-scheme）：深色宿主下
   *  iframe 文档不声明就地变画布白底（悬浮件圆角四角出方块），见 bootstrapJs。
   *  未知主题按深色（应用默认主题是 dark，themeService 缺省同口径）。 */
  const themeScheme: 'light' | 'dark' =
    effectiveTheme.config?.category === 'light' ? 'light' : 'dark'
  // 取数时点量（wrapHtml 注入首帧 meta 用）：主题切换不重取 HTML——由
  // theme.sync 下行桥即时纠正文档 color-scheme（bootstrap 监听器落地）
  const themeSchemeRef = useRef(themeScheme)
  themeSchemeRef.current = themeScheme

  // fetch 插件 HTML（带 Bearer token，token 不进 iframe URL）。
  // 状态机契约：终态显式可见——成功落 html；失败/挂起超时落 error（附重试
  // 按钮），绝不静默停留「加载 Webview...」占位。
  useEffect(() => {
    if (!endpoint) {
      if (!externalFramePath) setError('WebviewWidget 缺少 pluginId')
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
        setTransientPending(false)
        setHtml(
          wrapHtml(
            typeof res.data === 'string' ? res.data : String(res.data),
            instanceToken,
            // 挂载时活跃会话（bootstrap ctx 预置；后续变化靠 ctx.sync 推）
            useSessionStore.getState().activeSessionId,
            // 页面声明 CSP 扩展（声明面 props，fail-closed 归一）
            normalizeCspExtension(csp),
            // 透明悬浮件才钉文档色彩方案：画布底形态（bg-[var(--web-canvas)]
            // 恒白）不注入，保持既有观感零变化
            transparent ? themeSchemeRef.current : undefined,
          ),
        )
      })
      .catch((e) => {
        window.clearTimeout(timer)
        if (cancelled || timedOut) return
        // message 可能为空串（空串越过 ?? 兜底会 falsy 回加载态），按真值兜底
        const msg = (e as Error).message || '加载插件 HTML 失败'
        // 瞬态（404/route not found = 插件路由注册窗口期）→ 加载语义：停留
        // 加载占位退避重取，不落错误态——路由不是缺失，是还没就绪（注册窗口
        // 期应显示加载中，2026-10-15 用户架构裁定）。上限后才落终态。调度就地
        // 于 catch（error 状态链 effect 会被加载态 cleanup 取消断链），计数走 ref
        if (/404|route not found/i.test(msg)) {
          if (autoRetriesRef.current < 4) {
            const n = autoRetriesRef.current + 1
            autoRetriesRef.current = n
            setTransientPending(true)
            setTimeout(() => {
              if (mountedRef.current) setAttempt((x) => x + 1)
            }, 1500 * n)
          } else {
            setTransientPending(false)
            setError(msg)
          }
          return
        }
        setTransientPending(false)
        setError(msg)
      })
    return () => {
      cancelled = true
      window.clearTimeout(timer)
      controller.abort()
    }
  }, [endpoint, attempt, csp, transparent])

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
        if (msg.method === 'webview.resize') {
          // 自适应尺寸上行桥（ADR 2026-10-14-message-card-body-form，通用容器
          // 能力非卡片专用）：autoHeight 实例按卡内页回报高度撑开容器（clamp
          // 界内），可选 width 同桥回报容器宽——悬浮槽等 shrink-to-fit 容器里
          // 100% 宽塌缩为 0，卡自报实宽才可见；未开启实例 fail-closed 回
          // error（不静默忽略）。
          if (!autoHeight) {
            sendDown('error', { message: 'webview.resize 已丢弃：本实例未开启自适应高度' })
            return
          }
          const height = clampWebviewHeight(
            (msg.params as { height?: unknown } | null)?.height,
          )
          if (height === null) {
            sendDown('error', { message: 'webview.resize 已丢弃：height 须为有限数值' })
            return
          }
          // 可选 width：缺省不动容器宽（既有纯高度卡零变化）；在场须有限数值
          const rawWidth = (msg.params as { width?: unknown } | null)?.width
          let width: number | undefined
          if (rawWidth !== undefined) {
            const clampedWidth = clampWebviewWidth(rawWidth)
            if (clampedWidth === null) {
              sendDown('error', { message: 'webview.resize 已丢弃：width 须为有限数值' })
              return
            }
            width = clampedWidth
            setAutoWidthPx(clampedWidth)
          }
          setAutoHeightPx(height)
          sendDown('result', { applied: true, height, ...(width !== undefined ? { width } : {}) })
          return
        }
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
        if (msg.method === 'workspace.fullscreen') {
          // 工作区全屏桥（白名单宿主侧方法）：插件页面进入游玩态时请求工作区
          // 全屏（如酒馆玩法页「新开一局」/进局——玩法页独占工作区而非嵌在小
          // 面板里）。载荷 {on?: boolean}：缺省/true=进入全屏，false=退出；
          // 非 boolean 非法载荷整包丢弃（fail-closed）。纯宿主行为，不经内核。
          const on = (msg.params as { on?: unknown } | null | undefined)?.on
          if (on !== undefined && typeof on !== 'boolean') {
            sendDown('error', { message: 'workspace.fullscreen 已丢弃：on 须为 boolean' })
            return
          }
          useUIStore.getState().setWorkspaceFullscreen(on !== false)
          sendDown('result', { applied: true, fullscreen: on !== false })
          return
        }
        if (msg.method === 'mode.possess') {
          // 人设接管附身桥协议（mode.possess，批 G② 通用化）：白名单宿主侧
          // 方法——模式面板附身成功后上行 {mode, card_id, name, avatar,
          // personaText?}；注入键从 registry 该模式声明 decl.persona.from 解析
          // （resolvePersonaInjectionKey）：resolved → 随档钉住（出生即钉，
          // D9 同形态）；undeclared（模式未声明 persona/未收录）→ 整包拒绝；
          // unreachable → 附身建立但 personaKey=null（发送链诚实降级不注入）。
          // 载荷校验 fail-closed：非法整包丢弃（零状态变更）并回 error。纯宿主
          // 行为，不经内核 transport。
          const resolution = await resolvePersonaInjectionKey(
            (msg.params as Record<string, unknown> | null)?.mode as string,
          )
          const personaKey =
            resolution.status === 'resolved'
              ? resolution.personaKey
              : resolution.status === 'unreachable'
                ? null
                : undefined
          if (personaKey === undefined) {
            sendDown('error', {
              message: 'mode.possess 已丢弃：该模式未声明 persona 接管（mode.yaml persona.from）',
            })
            return
          }
          const applied = usePersonaPossessStore.getState().setPossessed(msg.params, personaKey)
          if (!applied) {
            sendDown('error', {
              message:
                'mode.possess 已丢弃：载荷需 mode（合法形态键）+ card_id/name 非空字符串 + avatar（emoji 或色对）',
            })
            return
          }
          // D8 提示义务（附身侧，一次建立提示一次，解除不提示）：附身替换
          // 系统提示词中的人设段（{{persona:}} 占位符换源），改变提示词前缀 →
          // 前缀缓存失效，后续对话 token 成本上升——附身是用户显式选择，代价
          // 须在建立时告知。
          const possessedCard = usePersonaPossessStore.getState().possessed
          useNotificationStore.getState().addNotification({
            // 附身档稳定 id（模式+卡）：同档重复上行（面板重载）同 id 去重，换卡/
            // 换模式是另一条提示——不走正文指纹闸（按实体语义去重，见 store 契约）
            id: `persona-possess:${possessedCard?.mode ?? ''}:${possessedCard?.card_id ?? ''}`,
            title: `已附身「${possessedCard?.name ?? ''}」`,
            message: '附身将接管人设，改变提示词前缀、破坏缓存命中（成本上升）。',
            priority: 'normal',
            category: 'info',
            isBlocking: false,
            autoDismissMs: 8000,
            sourceLabel: '人设接管',
          })
          sendDown('result', { applied: true })
          return
        }
        if (msg.method === 'mode.session') {
          // 模式会话出生桥协议（mode.session，批 G② 通用化）：白名单宿主侧
          // 方法——模式面板以模式开新会话：modeSessionBinder 出生通道（建会话
          // 随建即跳转 + modeBinding 快照 + 扩展位 agent_id/extra_context/
          // first_message + D8 人设接管提示 + 面板页随开）。载荷 fail-closed：
          // mode 形态键必填，扩展位在场须非空（agent_id 在场须携 name），非法
          // 整包丢弃（零状态变更）并回 error；card_theme 档非法只丢该位（出生
          // 零阻断）。回执带 theme_pinned 事实位（出生是否钉入卡主题）。
          const payload = parseModeSessionPayload(msg.params)
          if (!payload) {
            sendDown('error', {
              message: 'mode.session 已丢弃：载荷需 mode（合法形态键）；扩展位在场须非空字符串',
            })
            return
          }
          // intoSessionId：'current' = 活跃会话（B8 管道标签：开进当前会话为
          // 子管道，会话出生语义不动）；其余值原样透传（显式会话 id）
          const intoSessionId =
            payload.intoSessionId === 'current'
              ? useSessionStore.getState().activeSessionId ?? undefined
              : payload.intoSessionId
          const created = await openModeSession(payload.mode, {
            agentId: payload.agentId,
            agentName: payload.agentName,
            title: payload.title,
            extraContext: payload.extraContext,
            firstMessage: payload.firstMessage,
            pipelineConfigId: payload.pipelineConfigId,
            cardTheme: payload.cardTheme,
            intoSessionId,
          })
          sendDown('result', {
            applied: true,
            session_id: created.sessionId,
            theme_pinned: created.themePinned,
          })
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
          // 命令白名单（2026-09-25 桥协议统一，方案 §3.4）：表外非 '/' method
          // 视为命令调用，只放行**本插件贡献的 command**（contributes.commands，
          // registry 按 pluginId 过滤）——封死"沙箱页面直呼任意插件命令"的跨
          // 插件面。内置命令白名单当前为空（无暴露给沙箱的全局命令需求）。
          if (findCommandOwner(msg.method) !== pluginId) {
            sendDown('error', {
              message: `method 不在命令白名单（仅限本插件贡献的命令）: ${msg.method}`,
            })
            return
          }
          // 命令约定：复用 command transport 同一端点（带 Bearer token）
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
  }, [widgetId, pluginId, instanceToken, autoHeight])

  // 下行：latest 变化时推给 iframe；html 就绪时以当时值补推一次（宿主先
  // 派发、iframe 后挂监听的窗口防丢——与 theme.sync「就绪后以当时值补推，
  // 不丢终态」同口径）
  useEffect(() => {
    if (!latest || !html || !iframeRef.current?.contentWindow) return
    const msg = buildWebviewMessage('widget.event', latest)
    // origin 用 '*' 是因为 sandbox iframe origin 是 'null'，需显式指定
    iframeRef.current.contentWindow.postMessage(msg, '*')
  }, [latest, html])

  // 下行桥 theme.sync（面板-宿主融合协议）：就绪时推当前生效主题的 token map，
  // 主题变化（全局切换/会话出入栈/插件皮肤）时重推；未就绪不推（__ready/load
  // 就绪后会以当时值补推，不丢终态）。scheme 同批推：透明悬浮件的 iframe 文档
  // color-scheme 随宿主明暗即时纠正（payload 字段只增不删）。
  useEffect(() => {
    if (!webviewReady) return
    iframeRef.current?.contentWindow?.postMessage(
      buildWebviewMessage('theme.sync', {
        tokens: buildWebviewThemeTokens(effectiveTheme.config, effectiveTheme.pluginVars),
        scheme: themeScheme,
      }),
      '*', // sandbox iframe origin='null'，同上
    )
  }, [webviewReady, effectiveTheme, themeScheme])

  // 下行桥 ctx.sync：就绪时推当前活跃会话与激活管道，会话/管道切换时重推
  // （无会话推空串）。pipelineId 供插件页按管道精确取数（如待办角标拉
  // /latest?pipeline_id=——后台多管道并行时"最近更新"不等于当前会话管道）
  useEffect(() => {
    if (!webviewReady) return
    iframeRef.current?.contentWindow?.postMessage(
      buildWebviewMessage('ctx.sync', {
        sessionId: activeSessionId ?? '',
        pipelineId: activePipelineId ?? '',
      }),
      '*',
    )
  }, [webviewReady, activeSessionId, activePipelineId])

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

  // 外部帧：向插件端点轮询就绪地址。外部站点由另一个进程伺服，冷启动可达
  // 数分钟，故轮询而非一次性取址；available 前不落错误态，除非端点给出原因
  // 或超窗（此时如实展示对方 reason，不静默停留加载占位）。
  useEffect(() => {
    if (!externalFramePath || !pluginId) return
    let cancelled = false
    const deadline = Date.now() + EXTERNAL_FRAME_TIMEOUT_MS
    const poll = async (): Promise<void> => {
      while (!cancelled) {
        try {
          const res = await apiClient.get<ExternalFrameState>(
            extUrl(pluginId, externalFramePath),
          )
          if (cancelled) return
          const data = res.data
          if (data?.available) {
            const url = normalizeExternalFrameUrl(data.url)
            if (!url) {
              // 端点声称就绪却给出不可用地址（非 http/https 或缺值）：声明面错误，
              // 落显式错误态——不静默重试，也不把坏 scheme 塞进 iframe.src
              setError('外部页面地址不可用（仅支持 http/https 绝对 URL）')
              return
            }
            setExternalFrameUrl(url)
            setError(null)
            return
          }
          if (!data?.starting && data?.reason) {
            setError(String(data.reason))
            return
          }
        } catch (e) {
          if (cancelled) return
          setError((e as Error).message || '外部页面探测失败')
          return
        }
        if (Date.now() > deadline) {
          if (!cancelled) setError('外部页面未在等待上限内就绪')
          return
        }
        await new Promise((resolve) => setTimeout(resolve, EXTERNAL_FRAME_POLL_MS))
      }
    }
    void poll()
    return () => {
      cancelled = true
    }
  }, [externalFramePath, pluginId, attempt])

  if (externalFramePath) {
    return (
      <div className="relative h-full w-full">
        {externalFrameUrl ? (
          <iframe
            src={externalFrameUrl}
            title={title ?? 'Webview'}
            className="absolute inset-0 h-full w-full border-0"
          />
        ) : (
          <div className="text-muted-foreground flex h-full flex-col items-center justify-center gap-2 text-sm">
            <div>{error ? '外部页面不可用' : '正在启动外部页面…'}</div>
            {error && <div className="max-w-xl text-center text-xs">{error}</div>}
            {error && (
              <Button
                variant="outline"
                size="sm"
                onClick={() => {
                  setError(null)
                  setAttempt((n) => n + 1)
                }}
              >
                重试
              </Button>
            )}
          </div>
        )}
      </div>
    )
  }

  if (error) {
    return (
      <div className="text-muted-foreground flex h-full flex-col items-center justify-center gap-3 text-sm">
        <div className="flex items-center">
          <FileWarning className="mr-2 h-5 w-5" />
          Webview 加载失败: {error}
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            autoRetriesRef.current = 0
            setTransientPending(false)
            setAttempt((n) => n + 1)
          }}
        >
          重试
        </Button>
      </div>
    )
  }

  if (!html) {
    return (
      <div className="text-muted-foreground flex h-full flex-col items-center justify-center gap-1 text-sm">
        <div>加载 Webview...</div>
        {transientPending && <div className="text-xs">插件路由注册中…</div>}
      </div>
    )
  }

  return (
    <div
      className={autoHeight ? 'relative w-full' : 'relative h-full w-full'}
      style={
        autoHeight
          ? {
              height: `${autoHeightPx}px`,
              ...(autoWidthPx !== null ? { width: `${autoWidthPx}px` } : {}),
            }
          : undefined
      }
    >
      <iframe
        ref={iframeRef}
        srcDoc={html}
        title={title ?? 'Webview'}
        // 关键安全：不开 allow-same-origin → iframe 独立 opaque origin，无法访问宿主 token
        sandbox="allow-scripts allow-forms allow-popups allow-modals"
        onLoad={() => setWebviewReady(true)}
        // transparent 声明（悬浮叠加形态）改透明底：插件页自带圆角透出宿主，
        // 缺省 canvas 底（全页面形态需要不透明画布）
        className={`absolute inset-0 border-0 ${transparent ? 'bg-transparent' : 'bg-[var(--web-canvas)]'}`}
        style={{ width: '100%', height: '100%' }}
      />
    </div>
  )
}

export default WebviewWidget
