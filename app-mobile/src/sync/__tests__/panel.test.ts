// @vitest-environment jsdom
/**
 * 同步状态面板行为测试（jsdom）：挂载/开关、状态展示（未配对/已配对）、
 * 立即同步、解除配对走确认、动作后状态刷新。入网经前端登录自动完成
 * （登录即入网），面板无输入面——登录态外的入网路径不存在。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SyncPanel } from '../panel'
import {
  PHONE_DEVICE_ID,
  makeEngine,
  stagedEnroll,
  type EngineFixture,
} from './helpers'

describe('同步状态面板', () => {
  let fx: EngineFixture
  let panel: SyncPanel

  beforeEach(() => {
    fx = makeEngine()
    panel = new SyncPanel({ engine: fx.engine, credentialBackend: () => 'local_storage' })
  })

  afterEach(() => {
    panel.unmount()
    document.body.innerHTML = ''
  })

  async function open() {
    panel.mount(document.body)
    await panel.open()
  }

  function shadow(): ShadowRoot {
    return document.querySelector('[data-testid="sync-shell-panel"]')!.shadowRoot!
  }

  function text(testId: string): string {
    return shadow().querySelector(`[data-testid="${testId}"]`)!.textContent ?? ''
  }

  it('浮动入口挂载幂等；打开后呈现未配对引导（无输码输入面）', async () => {
    panel.mount(document.body)
    panel.mount(document.body)
    expect(document.querySelectorAll('#lingxi-sync-chip')).toHaveLength(1)
    await open()
    expect(document.querySelectorAll('[data-testid="sync-shell-panel"]')).toHaveLength(1)
    expect(text('sync-shell-status')).toContain('未配对')
    expect(text('sync-shell-status')).toContain('登录后自动入网')
    expect(shadow().querySelector('[data-testid="join-code-input"]')).toBeNull()
    expect(text('sync-shell-detail')).toContain('凭据通道 local_storage')
  })

  it('已配对：状态面出现操作区（立即同步/解除配对）', async () => {
    stagedEnroll(fx.relay)
    fx.relay.enqueueBootstrap({ device: PHONE_DEVICE_ID, events: [], cursor: 4 })
    await fx.engine.joinWithAuth('jwt-admin')
    await open()
    expect(text('sync-shell-status')).toContain('已配对')
    expect((shadow().querySelector('[data-testid="paired-actions"]') as HTMLElement).style.display).toBe('')
  })

  it('立即同步触发引擎单拍并反馈完成', async () => {
    stagedEnroll(fx.relay)
    fx.relay.enqueueBootstrap({ device: PHONE_DEVICE_ID, events: [], cursor: 2 })
    await fx.engine.joinWithAuth('jwt-admin')
    await open()
    ;(shadow().querySelector('[data-testid="sync-now-button"]') as HTMLButtonElement).click()
    await vi.waitFor(() => {
      expect(text('sync-now-result')).toBe('已完成本轮同步')
    })
    expect(fx.relay.heartbeatCalls).toHaveLength(1)
  }, 20_000)

  it('解除配对：确认后凭据清空、状态回未配对', async () => {
    stagedEnroll(fx.relay)
    fx.relay.enqueueBootstrap({ device: PHONE_DEVICE_ID, events: [], cursor: 2 })
    await fx.engine.joinWithAuth('jwt-admin')
    await open()
    window.confirm = () => true
    ;(shadow().querySelector('[data-testid="unpair-button"]') as HTMLButtonElement).click()
    await vi.waitFor(() => {
      expect(text('sync-shell-status')).toContain('未配对')
    })
    expect(await fx.credential.load()).toBeNull()
  })

  it('解除配对可取消（confirm=false 状态不动）', async () => {
    stagedEnroll(fx.relay)
    fx.relay.enqueueBootstrap({ device: PHONE_DEVICE_ID, events: [], cursor: 2 })
    await fx.engine.joinWithAuth('jwt-admin')
    await open()
    window.confirm = () => false
    ;(shadow().querySelector('[data-testid="unpair-button"]') as HTMLButtonElement).click()
    await new Promise((resolveTick) => setTimeout(resolveTick, 20))
    expect(await fx.credential.load()).not.toBeNull()
    expect(text('sync-shell-status')).toContain('已配对')
  })

  it('关闭与编程入口：close 移除面板，toggle 再开', async () => {
    await open()
    ;(shadow().querySelector('[data-testid="panel-close-button"]') as HTMLButtonElement).click()
    expect(document.querySelector('[data-testid="sync-shell-panel"]')).toBeNull()
    await panel.open()
    expect(document.querySelector('[data-testid="sync-shell-panel"]')).not.toBeNull()
  })
})
