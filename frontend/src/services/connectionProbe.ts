/**
 * 连接探活与内核版本查询（连接设置页 / 关于页共用）。
 *
 * 探活契约：GET {base}/health，2xx 即通（内核现成存活探针，无鉴权）。
 * 部署面限制：云端 nginx 不反代 /health（回落 SPA HTML，仍 200）——此时
 * 连接判通但 kernelVersion 不可得（null），关于页据此显示降级文案；
 * 内核直连部署（桌面装机 / 直连基址）返回 JSON，version 即内核版本。
 */

export interface ConnectionProbeResult {
  /** 2xx 即通 */
  ok: boolean
  /** HTTP 状态码（网络失败/超时为 0） */
  status: number
  /** 内核版本（/health 返回内核 JSON 时有值） */
  kernelVersion: string | null
  /** 往返耗时（毫秒） */
  latencyMs: number
}

/** 地址校验结果：url=null 表示清除用户设置（空输入，回落壳注入/同源） */
export type ServerBaseUrlValidation =
  | { ok: true; url: string | null }
  | { ok: false; reason: string }

const HTTP_SCHEME_RE = /^https?:\/\//i
const ANY_SCHEME_RE = /^([a-z][a-z0-9+.-]*):\/\//i

/**
 * 校验并规整用户输入的服务器地址：去首尾空白与结尾斜杠；无协议时补 http://；
 * 带协议但非 http/https（file:、ftp:、ws: 等）拒绝——后端连接只走 HTTP(S)。
 * 空输入返回 url=null（语义 = 清除用户设置，回落壳注入/同源）。
 */
export function validateServerBaseUrl(input: string): ServerBaseUrlValidation {
  const trimmed = input.trim().replace(/\/+$/, '')
  if (!trimmed) return { ok: true, url: null }
  const schemeMatch = trimmed.match(ANY_SCHEME_RE)
  if (schemeMatch && !HTTP_SCHEME_RE.test(trimmed)) {
    return { ok: false, reason: `不支持的协议「${schemeMatch[1]}:」，仅支持 http/https` }
  }
  return { ok: true, url: HTTP_SCHEME_RE.test(trimmed) ? trimmed : `http://${trimmed}` }
}

// WHATWG URL 的 hostname 对 IPv6 保留方括号（[::1]），回环判定须含括号形态
const LOOPBACK_HOST_RE =
  /^(localhost$|127\.\d+\.\d+\.\d+$|::1$|::ffff:127\.\d+\.\d+\.\d+$|\[::1\]$)/

/**
 * 是否「远程明文」地址（P6 连接安全铁律的呈现面判定）：http 协议且目标主机
 * 非回环——明文仅限 127.0.0.1 与过渡期，远程 http 保存时 UI 须提示。
 */
export function isPlaintextRemoteUrl(url: string): boolean {
  try {
    const parsed = new URL(url)
    if (parsed.protocol !== 'http:') return false
    return !LOOPBACK_HOST_RE.test(parsed.hostname.toLowerCase())
  } catch {
    return false
  }
}

/**
 * 回环源站判定（http://127.*|localhost|::1）：他机必不可达。设备页据此不渲染
 * 死连接卡/不给死按钮（远端地址卡与注册表设备卡同律）：所连内核 relay 配在
 * 自己 loopback 上时该地址只对本机有意义，本机连接走本机卡不经远端卡。
 */
export function isLoopbackOrigin(origin: string): boolean {
  try {
    return LOOPBACK_HOST_RE.test(new URL(origin).hostname.toLowerCase())
  } catch {
    return false
  }
}

export function probeConnection(base: string, timeoutMs = 5000): Promise<ConnectionProbeResult> {
  const started = Date.now()
  const finish = (ok: boolean, status: number, kernelVersion: string | null) => ({
    ok,
    status,
    kernelVersion,
    latencyMs: Date.now() - started,
  })
  return fetch(`${base}/health`, { signal: AbortSignal.timeout(timeoutMs) })
    .then(async (resp) => {
      // 非 JSON 响应（网关回落 SPA HTML）按版本不可得处理，连接判定不受影响
      let kernelVersion: string | null = null
      try {
        const data: unknown = await resp.json()
        if (data && typeof data === 'object' && 'version' in data) {
          const v = (data as { version?: unknown }).version
          if (typeof v === 'string' && v) kernelVersion = v
        }
      } catch {
        // 响应体非 JSON：保持 kernelVersion=null（翻译为值，不阻塞连接判定）
      }
      return finish(resp.status >= 200 && resp.status < 300, resp.status, kernelVersion)
    })
    .catch(() => finish(false, 0, null))
}
