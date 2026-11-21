/**
 * 登录失败三分类与横幅文案（S5-A②/S5-D①）。
 *
 * 判定只认稳定契约——拦截器 reject 时附着在 Error 上的机器码/HTTP 状态串/
 * axios 错误码（services/api/client.ts rejectWithError），刻意不做文案匹配
 * （文案随版本漂移）。消费方：authStore.login（横幅文案单点）与 LoginModal
 * 本机免填自动链（429 终态停链）。
 */

/** 登录失败类别：限速锁（终态）/ 凭据错（漂移态）/ 网络不可达 / 其余 */
export type LoginFailureKind = 'rate_limited' | 'credential' | 'network' | 'unknown'

export interface LoginFailure {
  kind: LoginFailureKind
  /**
   * network 类的细分：true=超时（请求发出但内核未在超时窗口内应答）、
   * false/缺省=连不上（连接被拒/网络不可达）。只影响文案，不影响 kind。
   */
  timedOut?: boolean
  /** 429 时内核 Retry-After 提示（秒，auth.rs TooManyRequests 恒带）；缺失为 undefined */
  retryAfterSecs?: number
}

/** 拦截器 reject 形状的公共子面（Error + 附着字段；非对象安全降级） */
interface LoginErrorShape {
  message?: unknown
  code?: unknown
  retry_after_secs?: unknown
}

function asShape(error: unknown): LoginErrorShape {
  if (typeof error !== 'object' || error === null) return {}
  return error as LoginErrorShape
}

/** 限速锁：内核机器码（统一信封）+ 无信封时的 HTTP 状态串回退 */
const RATE_LIMITED_CODES = new Set(['TOO_MANY_REQUESTS', '429'])
/** 凭据错：/auth/login 凭据不匹配族（内核 BadRequest「用户名或密码错误」+ 401 族回退） */
const CREDENTIAL_CODES = new Set(['BAD_REQUEST', 'UNAUTHORIZED', '400', '401'])
/** 网络失败：无 HTTP 响应的 axios 错误码（超时/断连） */
const NETWORK_CODES = new Set(['ERR_NETWORK', 'ECONNABORTED', 'ETIMEDOUT'])
/** 网络类中的"超时"子族：请求已发出、内核未在窗口内应答（与连不上区分） */
const TIMEOUT_CODES = new Set(['ECONNABORTED', 'ETIMEDOUT'])

/** 分类登录失败错误（总判定入口；未知形状一律 unknown 不误判） */
export function classifyLoginFailure(error: unknown): LoginFailure {
  const e = asShape(error)
  const code = typeof e.code === 'string' ? e.code : ''
  if (RATE_LIMITED_CODES.has(code)) {
    return {
      kind: 'rate_limited',
      retryAfterSecs: typeof e.retry_after_secs === 'number' ? e.retry_after_secs : undefined,
    }
  }
  if (CREDENTIAL_CODES.has(code)) return { kind: 'credential' }
  if (NETWORK_CODES.has(code) || e.message === 'Network Error') {
    // 超时（ECONNABORTED/ETIMEDOUT）= 内核在但没及时答（装配/繁忙期整体停顿）；
    // ERR_NETWORK = 根本连不上。两者用户该做的事不同（等一等重试 / 查服务是否起），
    // 故分类保留，文案分流。
    return { kind: 'network', timedOut: TIMEOUT_CODES.has(code) }
  }
  return { kind: 'unknown' }
}

/**
 * 三类横幅文案（区别呈现，不共用一条话术）：
 * - 429=锁定终态：告知已锁定 + 等待秒数 + 重启逃生口，不诱导继续输入（内核
 *   锁窗口内手输正确口令同样 429，诱导重试只会重演「输对了也登不进」）
 * - 网络失败=无法连接内核 + 附当前目标基址（本机内核未起与远程不可达由目标
 *   可辨别，S5-D①）；无基址（同源）不输出空目标段
 * - 凭据错/未知=后端原文透传，原文缺失回退固定文案
 */
export function loginFailureBanner(
  failure: LoginFailure,
  apiBase: string,
  rawMessage?: string,
): string {
  switch (failure.kind) {
    case 'rate_limited':
      return failure.retryAfterSecs !== undefined
        ? `尝试过多已锁定，请等待 ${failure.retryAfterSecs} 秒或重启应用`
        : '尝试过多已锁定，请等待锁定期结束或重启应用'
    case 'network': {
      const target = apiBase ? `（目标：${apiBase}）` : ''
      // 超时=内核在但没及时应答（装配/繁忙期整体停顿，实测停顿可达 60s 级）：
      // 让用户"再点一次"才是有效动作，说"无法连接"会误导去查服务是否启动。
      if (failure.timedOut) {
        return `内核响应超时${target}（启动中或繁忙），请再点一次登录`
      }
      return `无法连接内核${target}，请确认服务已启动或检查服务器地址`
    }
    case 'credential':
      return rawMessage || '用户名或密码错误'
    default:
      return rawMessage || '登录失败'
  }
}
