/**
 * 测试环境全局设置
 *
 * 配置 testing-library/jest-dom 的自定义匹配器
 */
import '@testing-library/jest-dom/vitest'

// ---------------------------------------------------------------------------
// localStorage / sessionStorage 内存 shim
// @feature FP-MIGR 0.1→0.2 迁移基建 | @audit T5 (本地复测新发现)
//
// 背景:jsdom 29 在 Node ≥25 下,vitest 传入 `--localstorage-file` 但路径无效
// (日志可见 "Warning: --localstorage-file was provided without a valid path"),
// 导致 jsdom 原生 localStorage 初始化损坏——`localStorage.clear is not a function`,
// 直接让 auth/client/tokenRefresh 等 ~20 个测试文件全挂(auth.test.ts:35、
// client.test.ts:12 等 beforeEach/afterEach 调用 localStorage.clear())。
// CI(Ubuntu + Node 20)无此问题;此 shim 让本地与 CI 行为一致。
// 用符合 Web Storage API 的内存实现兜底,不依赖任何文件路径。
// ---------------------------------------------------------------------------
class MemoryStorage implements Storage {
  private store = new Map<string, string>()
  get length(): number {
    return this.store.size
  }
  clear(): void {
    this.store.clear()
  }
  getItem(key: string): string | null {
    return this.store.has(key) ? (this.store.get(key) as string) : null
  }
  key(index: number): string | null {
    return Array.from(this.store.keys())[index] ?? null
  }
  removeItem(key: string): void {
    this.store.delete(key)
  }
  setItem(key: string, value: string): void {
    this.store.set(key, String(value))
  }
}

const installStorage = (prop: 'localStorage' | 'sessionStorage') => {
  const shim = new MemoryStorage()
  try {
    Object.defineProperty(window, prop, { value: shim, writable: true, configurable: true })
  } catch {
    // 某些环境下 window 属性不可重定义,退而覆盖 globalThis
    ;(globalThis as unknown as Record<string, unknown>)[prop] = shim
  }
}
installStorage('localStorage')
installStorage('sessionStorage')

// ---------------------------------------------------------------------------
// antd 组件在 jsdom 下需要的浏览器 API polyfill
//
// matchMedia：antd Grid/useBreakpoint 初始化即调用；ResizeObserver：rc-* 系列
// （Select/DatePicker/虚拟滚动）挂载时调用。jsdom 均不实现，缺失会导致
// 引入真 antd 组件的用例（RjsfForm 表单等）直接抛错。
// ---------------------------------------------------------------------------
if (typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => {},
      removeListener: () => {},
      addEventListener: () => {},
      removeEventListener: () => {},
      dispatchEvent: () => false,
    }),
  })
}

if (typeof globalThis.ResizeObserver !== 'function') {
  class ResizeObserverStub implements Partial<ResizeObserver> {
    observe(): void {}
    unobserve(): void {}
    disconnect(): void {}
  }
  ;(globalThis as unknown as { ResizeObserver: typeof ResizeObserverStub }).ResizeObserver =
    ResizeObserverStub
}
