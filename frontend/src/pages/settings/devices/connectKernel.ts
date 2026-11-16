/**
 * 连接内核动作（连接切换唯一写点）：写 server_base_url（用户设置，优先级最高）
 * + 上报后端模式（remote=壳原生栈停本地内核）+ 整页刷新收尾（API_BASE_URL 是
 * 模块加载期常量，刷新后新地址才生效）。远端地址卡、注册表设备卡、登录框
 * 服务器表单、本机卡切回——全部入口同路归此（单一连接语义，2026-10-23 用户令）。
 *
 * 探活闸（2026-10-22 用户实测三撞）：切换前先 GET {address}/health，不可达即
 * 拒绝切换并回传错误——死目标切过去=整页落在远端档登录闸（无免填凭据、目标
 * 已死连输账号都无处提交），是「连个死地址把应用打回登录」的直接根源。本机档
 * 目标不探活：本机内核随应用生命周期，且壳冷启动窗口内探活必假阴。
 *
 * 本机档目标 = 空地址或回环地址（127.x/localhost/::1）：回环就是设备自己的
 * 内核，按切回本机同路处理（2026-10-23 用户令）——不再判「自定义后端」（那会
 * 停本机栈 + 本机免填凭据失效，把用户锁进手输随机口令的死路）。
 */
import { isLoopbackOrigin, probeConnection, validateServerBaseUrl } from '@/services/connectionProbe'
import { SERVER_BASE_URL_KEY } from '@/services/shellApiBase'
import { applyShellBackendMode } from '@/services/shellBackendMode'

/** 刷新收尾延迟：给提示文案留可读窗口（本机卡切回同窗复用） */
export const RELOAD_DELAY_MS = 1200

export type ConnectKernelResult = { ok: true } | { ok: false; error: string }

export async function connectKernel(address: string): Promise<ConnectKernelResult> {
  const validation = validateServerBaseUrl(address)
  if (!validation.ok) {
    return { ok: false, error: validation.reason }
  }

  // 本机档目标 = 空地址或回环地址（回环就是设备自己的内核，归并切回本机同路）；
  // remoteUrl 归一化后只剩「真的要切过去的远端目标」。
  const url = validation.url
  const remoteUrl = url !== null && !isLoopbackOrigin(url) ? url : null

  if (remoteUrl !== null) {
    const probe = await probeConnection(remoteUrl)
    if (!probe.ok) {
      return {
        ok: false,
        error: `目标内核不可达（${remoteUrl}）：未切换连接，请确认该设备内核正在运行`,
      }
    }
  }

  try {
    if (remoteUrl === null) {
      localStorage.removeItem(SERVER_BASE_URL_KEY)
    } else {
      localStorage.setItem(SERVER_BASE_URL_KEY, remoteUrl)
    }
  } catch {
    return { ok: false, error: '本地存储不可用，未切换连接' }
  }
  applyShellBackendMode(remoteUrl === null ? 'local' : 'remote')
  window.setTimeout(() => window.location.reload(), RELOAD_DELAY_MS)
  return { ok: true }
}
