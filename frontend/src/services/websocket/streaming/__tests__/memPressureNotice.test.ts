// @feature: FP-0.2.可观测性 内存压力告警专项呈现（ADR 2026-10-19 D5 第一级） | @ci: frontend-test
/**
 * handleMemPressureNotice 测试：mem_pressure / mem_pressure_cleared
 * system_notification → 专项 toast（error 醒目带观测数字 / success 轻呈现）；
 * 非 mem 族零触碰（透传返回 false，由调用方落插件健康面）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
const { toastError, toastSuccess } = vi.hoisted(() => ({
  toastError: vi.fn(),
  toastSuccess: vi.fn(),
}))
vi.mock('@/components/ui/sonner', () => ({
  toast: {
    error: toastError,
    warning: vi.fn(),
    info: vi.fn(),
    success: toastSuccess,
  },
}))
import {
  buildMemPressureMessage,
  formatGb,
  formatWan,
  handleMemPressureNotice,
  MEM_PRESSURE_CLEARED_KIND,
  MEM_PRESSURE_KIND,
  parseMemObservation,
  resolveMemPressureKind,
} from '../memPressureNotice'

/** 后端 plugin_notices.payload 契约样例（kernel/crates/api/src/plugin_notices.rs，
 * 正文样例对齐 main.rs 采样循环 detail 格式） */
function memEvent(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    data: {
      pipeline_id: 'pipe-1',
      thread_id: 'thread-1',
      record_id: 'n_plugin_unavailable_mem_pressure_1730000000000',
      notification_id: MEM_PRESSURE_KIND,
      notificationType: MEM_PRESSURE_KIND,
      level: 'error',
      content:
        '内核内存压力告警：进程 commit 25702MB（WARN 阈值 8192MB）、' +
        'abandoned 460000 页（阈值 50000）超限——宿主重生风暴或线程 churn 嫌疑；' +
        '已收紧清扫节奏，超预算时新增宿主 spawn 将被拒绝',
      sequence: null,
      ...overrides,
    },
  }
}

function clearedEvent(overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return memEvent({
    record_id: 'n_plugin_unavailable_mem_pressure_cleared_1730000000600',
    notification_id: MEM_PRESSURE_CLEARED_KIND,
    notificationType: MEM_PRESSURE_CLEARED_KIND,
    level: 'success',
    content: '内核内存压力解除：进程 commit 512MB、abandoned 3000 页已回落 WARN 阈值下，恢复常规节奏',
    ...overrides,
  })
}

beforeEach(() => {
  toastError.mockClear()
  toastSuccess.mockClear()
})

describe('resolveMemPressureKind kind 判定', () => {
  it('notificationType 权威：mem_pressure / mem_pressure_cleared 各归各', () => {
    expect(resolveMemPressureKind('mem_pressure', 'n_plugin_unavailable_x_1')).toBe('mem_pressure')
    expect(resolveMemPressureKind('mem_pressure_cleared', 'n_plugin_unavailable_x_1')).toBe(
      'mem_pressure_cleared',
    )
  })

  it('notificationType 缺省：从 record_id kind 段回退解析', () => {
    expect(
      resolveMemPressureKind(undefined, 'n_plugin_unavailable_mem_pressure_1730000000000'),
    ).toBe('mem_pressure')
    expect(
      resolveMemPressureKind('', 'n_plugin_unavailable_mem_pressure_cleared_1730000000600'),
    ).toBe('mem_pressure_cleared')
  })

  it('未知 kind / 非 mem 通知 / 缺 record_id → null（透传不截胡）', () => {
    expect(resolveMemPressureKind('verify_failed', 'n_plugin_unavailable_verify_failed_1')).toBeNull()
    expect(resolveMemPressureKind('task_done', 'rec_20261019_001')).toBeNull()
    expect(resolveMemPressureKind(undefined, 'rec_20261019_001')).toBeNull()
    expect(resolveMemPressureKind(undefined, undefined)).toBeNull()
  })

  it('record_id 形似但 kind 段不符（前缀拼接攻击面）不误判', () => {
    expect(resolveMemPressureKind(undefined, 'n_plugin_unavailable_mem_pressureX123')).toBeNull()
    expect(resolveMemPressureKind(undefined, 'n_plugin_unavailable_mem_pressurefoo_1')).toBeNull()
  })
})

describe('formatGb / formatWan 数字格式化', () => {
  it('MB→GB：整值去尾零，非整值保 1 位小数', () => {
    expect(formatGb(8192)).toBe('8GB')
    expect(formatGb(25702)).toBe('25.1GB')
    expect(formatGb(1024)).toBe('1GB')
    expect(formatGb(0)).toBe('0GB')
  })

  it('页→万：整值去尾零，非整值保 1 位小数', () => {
    expect(formatWan(50000)).toBe('5万')
    expect(formatWan(460000)).toBe('46万')
    expect(formatWan(46000)).toBe('4.6万')
    expect(formatWan(0)).toBe('0万')
  })
})

describe('parseMemObservation 正文观测解析', () => {
  it('进入沿正文：观测对阈值四数全提', () => {
    const obs = parseMemObservation(
      '内核内存压力告警：进程 commit 25702MB（WARN 阈值 8192MB）、' +
        'abandoned 460000 页（阈值 50000）超限——宿主重生风暴或线程 churn 嫌疑',
    )
    expect(obs).toEqual({
      commitMb: 25702,
      abandonedPages: 460000,
      thresholdCommitMb: 8192,
      thresholdAbandonedPages: 50000,
    })
  })

  it('退出沿正文：只有观测数，阈值为 undefined', () => {
    const obs = parseMemObservation(
      '内核内存压力解除：进程 commit 512MB、abandoned 3000 页已回落 WARN 阈值下，恢复常规节奏',
    )
    expect(obs.commitMb).toBe(512)
    expect(obs.abandonedPages).toBe(3000)
    expect(obs.thresholdCommitMb).toBeUndefined()
    expect(obs.thresholdAbandonedPages).toBeUndefined()
  })

  it('内核采样缺数（非数字占位）与格式漂移：缺数不炸', () => {
    expect(parseMemObservation('进程 commit 采样缺数MB、abandoned 采样缺数 页')).toEqual({
      commitMb: undefined,
      abandonedPages: undefined,
      thresholdCommitMb: undefined,
      thresholdAbandonedPages: undefined,
    })
    expect(parseMemObservation('完全不相干的正文')).toEqual({
      commitMb: undefined,
      abandonedPages: undefined,
      thresholdCommitMb: undefined,
      thresholdAbandonedPages: undefined,
    })
  })
})

describe('buildMemPressureMessage 文案组装', () => {
  it('有数：error 级，title 含「内存高压」+ GB/万 格式化观测对', () => {
    const msg = buildMemPressureMessage(
      MEM_PRESSURE_KIND,
      '内核内存压力告警：进程 commit 25702MB（WARN 阈值 8192MB）、' +
        'abandoned 460000 页（阈值 50000）超限——宿主重生风暴嫌疑',
    )
    expect(msg.severity).toBe('error')
    expect(msg.title).toBe('内存高压：commit 25.1GB / 废弃页 46万（阈值 8GB / 5万）')
    expect(msg.description).toContain('宿主重生风暴嫌疑')
  })

  it('缺数：title 落 "—" 不炸（内核采样缺数/正文漂移）', () => {
    const msg = buildMemPressureMessage(MEM_PRESSURE_KIND, '内核内存压力告警：进程 commit 采样缺数MB')
    expect(msg.title).toBe('内存高压：commit — / 废弃页 —（阈值 — / —）')
    expect(msg.severity).toBe('error')
  })

  it('解除沿：success 级轻呈现，title「内存压力解除」，正文透传', () => {
    const content = '内核内存压力解除：进程 commit 512MB、abandoned 3000 页已回落 WARN 阈值下，恢复常规节奏'
    const msg = buildMemPressureMessage(MEM_PRESSURE_CLEARED_KIND, content)
    expect(msg.severity).toBe('success')
    expect(msg.title).toBe('内存压力解除')
    expect(msg.description).toBe(content)
  })
})

describe('handleMemPressureNotice toast 与消费判定', () => {
  it('进入沿：error toast（title 带数字，id=record_id 幂等）', () => {
    expect(handleMemPressureNotice(memEvent())).toBe(true)

    expect(toastError).toHaveBeenCalledTimes(1)
    expect(toastSuccess).not.toHaveBeenCalled()
    const [title, options] = toastError.mock.calls[0] as [string, { description?: string; id?: string }]
    expect(title).toBe('内存高压：commit 25.1GB / 废弃页 46万（阈值 8GB / 5万）')
    expect(options.description).toContain('WARN 阈值 8192MB')
    expect(options.id).toBe('n_plugin_unavailable_mem_pressure_1730000000000')
  })

  it('解除沿：success toast 轻呈现', () => {
    expect(handleMemPressureNotice(clearedEvent())).toBe(true)

    expect(toastSuccess).toHaveBeenCalledTimes(1)
    expect(toastError).not.toHaveBeenCalled()
    const [title, options] = toastSuccess.mock.calls[0] as [string, { description?: string; id?: string }]
    expect(title).toBe('内存压力解除')
    expect(options.id).toBe('n_plugin_unavailable_mem_pressure_cleared_1730000000600')
  })

  it('WS 重连重放（同 record_id）：再次消费、同 id 幂等不刷屏', () => {
    handleMemPressureNotice(memEvent())
    handleMemPressureNotice(memEvent())

    expect(toastError).toHaveBeenCalledTimes(2)
    const [, opts1] = toastError.mock.calls[0] as [string, { id?: string }]
    const [, opts2] = toastError.mock.calls[1] as [string, { id?: string }]
    expect(opts1.id).toBe(opts2.id)
  })

  it('未知 kind 透传：返回 false 不弹 toast（调用方落插件健康面）', () => {
    expect(
      handleMemPressureNotice(
        memEvent({ record_id: 'n_plugin_unavailable_verify_failed_1', notificationType: 'verify_failed', notification_id: 'verify_failed' }),
      ),
    ).toBe(false)
    expect(
      handleMemPressureNotice(memEvent({ record_id: 'rec_task_done_1', notificationType: 'task_done', notification_id: 'task_done' })),
    ).toBe(false)

    expect(toastError).not.toHaveBeenCalled()
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('契约外形态（缺 record_id / 缺正文 / 非对象）：不弹不炸', () => {
    const noRecord = memEvent() as { data: Record<string, unknown> }
    delete noRecord.data.record_id
    expect(handleMemPressureNotice(noRecord)).toBe(false)
    expect(handleMemPressureNotice(memEvent({ content: '' }))).toBe(false)
    expect(handleMemPressureNotice({ data: 42 })).toBe(false)
    expect(handleMemPressureNotice(null)).toBe(false)
    expect(handleMemPressureNotice(undefined)).toBe(false)

    expect(toastError).not.toHaveBeenCalled()
    expect(toastSuccess).not.toHaveBeenCalled()
  })

  it('扁平载荷（无 data 包裹，全局广播形态）同等处理', () => {
    const wrapped = memEvent() as { data: Record<string, unknown> }
    expect(handleMemPressureNotice({ ...wrapped.data })).toBe(true)

    expect(toastError).toHaveBeenCalledTimes(1)
  })
})
