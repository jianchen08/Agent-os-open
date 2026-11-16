// @feature: FP-0.2.可观测性 插件不可用系统通知（卡3 前端） | @ci: frontend-test
/**
 * handlePluginUnavailableNotice 测试：插件不可用 system_notification →
 * toast（按 level 定严重度）+ 降级横幅登记；非健康类通知零触碰（与既有
 * 气泡路径/通知中心互不干扰）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
const { toastError, toastWarning, toastInfo } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastWarning: vi.fn(),
  toastInfo: vi.fn(),
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: {
    error: toastError,
    warning: toastWarning,
    info: toastInfo,
    success: vi.fn(),
  },
}))
import { useNotificationStore } from '@/stores/notificationStore'
import { usePluginHealthStore } from '@/stores/pluginHealthStore'
import {
  extractNoticeSubject,
  handlePluginUnavailableNotice,
  isPluginUnavailableNotice,
  PLUGIN_NOTICE_RECORD_PREFIX,
  toastSeverityForLevel,
} from '../pluginHealthNotice'

/** 后端 plugin_notices.payload 契约样例（kernel/crates/api/src/plugin_notices.rs） */
function noticeEvent(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    data: {
      pipeline_id: 'pipe-1',
      thread_id: 'thread-1',
      record_id: 'n_plugin_unavailable_verify_failed_1730000000000',
      notification_id: 'verify_failed',
      notificationType: 'verify_failed',
      level: 'error',
      content: '插件组 light 成员 task_submit当前不可用（ImportError: x），3 个工具已下线并从工具面遮挡；修复插件后将自动恢复，详见插件管理页。',
      sequence: null,
      ...overrides,
    },
  }
}

beforeEach(() => {
  localStorage.clear()
  usePluginHealthStore.setState({ unavailable: [] })
  useNotificationStore.setState({ notifications: [] })
  toastError.mockClear()
  toastWarning.mockClear()
  toastInfo.mockClear()
})

describe('isPluginUnavailableNotice 健康类识别', () => {
  it('record_id 前缀 n_plugin_unavailable_ 命中', () => {
    expect(isPluginUnavailableNotice(noticeEvent())).toBe(true)
  })

  it('普通系统通知（其他 record_id）不命中', () => {
    expect(isPluginUnavailableNotice(noticeEvent({ record_id: 'rec_20261017_001' }))).toBe(false)
  })

  it('缺 record_id 不命中（对齐 handleSystemNotification 拒渲染契约）', () => {
    const ev = noticeEvent() as { data: Record<string, unknown> }
    delete ev.data.record_id
    expect(isPluginUnavailableNotice(ev)).toBe(false)
    expect(isPluginUnavailableNotice(null)).toBe(false)
    expect(isPluginUnavailableNotice(undefined)).toBe(false)
  })

  it('data 为非对象真值（契约外形态）不命中也不炸', () => {
    expect(isPluginUnavailableNotice({ data: 42 })).toBe(false)
    expect(handlePluginUnavailableNotice({ data: 42 })).toBeUndefined()
    expect(usePluginHealthStore.getState().unavailable).toHaveLength(0)
  })
})

describe('toastSeverityForLevel level→严重度', () => {
  it('error→error / warning→warning / info→info / 未知→info', () => {
    expect(toastSeverityForLevel('error')).toBe('error')
    expect(toastSeverityForLevel('warning')).toBe('warning')
    expect(toastSeverityForLevel('info')).toBe('info')
    expect(toastSeverityForLevel('critical')).toBe('error')
    expect(toastSeverityForLevel(undefined)).toBe('info')
  })
})

describe('extractNoticeSubject 主体解析', () => {
  it('取「当前不可用」前的主语（组名/成员）', () => {
    expect(extractNoticeSubject('插件组 light 成员 task_submit当前不可用（x）；详见插件管理页。'))
      .toBe('插件组 light 成员 task_submit')
    expect(extractNoticeSubject('插件 fs_tools当前不可用（y）'))
      .toBe('插件 fs_tools')
  })

  it('正文无「当前不可用」标记：全文兜底（格式漂移不丢主体）', () => {
    expect(extractNoticeSubject('自由格式正文')).toBe('自由格式正文')
    expect(extractNoticeSubject('')).toBe('')
  })
})

describe('handlePluginUnavailableNotice toast 与登记', () => {
  it('健康类通知：按 level 弹 error toast（id=record_id 幂等）+ 横幅登记', () => {
    handlePluginUnavailableNotice(noticeEvent())

    expect(toastError).toHaveBeenCalledTimes(1)
    const [message, options] = toastError.mock.calls[0] as [string, { description?: string; id?: string }]
    expect(message).toBe('插件组 light 成员 task_submit')
    expect(options.description).toContain('当前不可用')
    expect(options.id).toBe('n_plugin_unavailable_verify_failed_1730000000000')

    const list = usePluginHealthStore.getState().unavailable
    expect(list).toHaveLength(1)
    expect(list[0].key).toBe('verify_failed|插件组 light 成员 task_submit')
  })

  it('level=warning → warning toast；level 缺省 → info toast', () => {
    handlePluginUnavailableNotice(
      noticeEvent({ level: 'warning', record_id: 'n_plugin_unavailable_spawn_breaker_1' }),
    )
    handlePluginUnavailableNotice(
      noticeEvent({ level: undefined, record_id: 'n_plugin_unavailable_spawn_breaker_2' }),
    )

    expect(toastWarning).toHaveBeenCalledTimes(1)
    expect(toastInfo).toHaveBeenCalledTimes(1)
    expect(toastError).not.toHaveBeenCalled()
  })

  it('notificationType 缺省：kind 从 record_id 解析回退（提取 kind 段）', () => {
    handlePluginUnavailableNotice(
      noticeEvent({ notificationType: undefined, notification_id: undefined }),
    )

    const list = usePluginHealthStore.getState().unavailable
    expect(list).toHaveLength(1)
    expect(list[0].kind).toBe('verify_failed')
    expect(list[0].key).toBe('verify_failed|插件组 light 成员 task_submit')
  })

  it('record_id 无 kind 段（契约外形态）：kind 回退 unknown，不丢登记', () => {
    handlePluginUnavailableNotice(
      noticeEvent({ record_id: PLUGIN_NOTICE_RECORD_PREFIX, notificationType: undefined }),
    )

    const list = usePluginHealthStore.getState().unavailable
    expect(list).toHaveLength(1)
    expect(list[0].kind).toBe('unknown')
  })

  it('同 record_id 重放（WS 重连补漏）：同 id 重入（sonner 按 id 幂等），横幅仍一行', () => {
    handlePluginUnavailableNotice(noticeEvent())
    handlePluginUnavailableNotice(noticeEvent())

    expect(usePluginHealthStore.getState().unavailable).toHaveLength(1)
    expect(toastError).toHaveBeenCalledTimes(2)
    const [, opts1] = toastError.mock.calls[0] as [string, { id?: string }]
    const [, opts2] = toastError.mock.calls[1] as [string, { id?: string }]
    expect(opts1.id).toBe(opts2.id)
  })

  it('去重窗外再报（新 record_id）：横幅 upsert 仍一行，toast 各归各实例（不同 id）', () => {
    handlePluginUnavailableNotice(noticeEvent())
    handlePluginUnavailableNotice(
      noticeEvent({ record_id: 'n_plugin_unavailable_verify_failed_1730000000600' }),
    )

    expect(usePluginHealthStore.getState().unavailable).toHaveLength(1)
    expect(toastError).toHaveBeenCalledTimes(2)
    const [, opts1] = toastError.mock.calls[0] as [string, { id?: string }]
    const [, opts2] = toastError.mock.calls[1] as [string, { id?: string }]
    expect(opts1.id).not.toBe(opts2.id)
  })

  it('非健康类系统通知：零触碰（不弹 toast、不进横幅）', () => {
    handlePluginUnavailableNotice(noticeEvent({ record_id: 'rec_task_done_1' }))

    expect(toastError).not.toHaveBeenCalled()
    expect(toastWarning).not.toHaveBeenCalled()
    expect(toastInfo).not.toHaveBeenCalled()
    expect(usePluginHealthStore.getState().unavailable).toHaveLength(0)
  })

  it('content 缺失：不登记不弹（无可展示主体）', () => {
    handlePluginUnavailableNotice(noticeEvent({ content: '' }))

    expect(toastError).not.toHaveBeenCalled()
    expect(usePluginHealthStore.getState().unavailable).toHaveLength(0)
  })

  it('content 非字符串（契约外形态）：不登记不弹', () => {
    handlePluginUnavailableNotice(noticeEvent({ content: 123 }))

    expect(toastError).not.toHaveBeenCalled()
    expect(usePluginHealthStore.getState().unavailable).toHaveLength(0)
  })

  it('扁平载荷（无 data 包裹）与包裹载荷同等处理', () => {
    const wrapped = noticeEvent() as { data: Record<string, unknown> }
    handlePluginUnavailableNotice({ ...wrapped.data })

    expect(toastError).toHaveBeenCalledTimes(1)
    expect(usePluginHealthStore.getState().unavailable).toHaveLength(1)
  })

  it('与通知中心不冲突：健康类通知不进 NotificationCenter 列表', () => {
    handlePluginUnavailableNotice(noticeEvent())

    expect(useNotificationStore.getState().notifications).toHaveLength(0)
  })
})
