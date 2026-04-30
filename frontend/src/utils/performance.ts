/**
 * 性能监控工具
 *
 * 使用 Web Vitals API 监控关键性能指标：
 * - CLS (Cumulative Layout Shift) - 布局偏移
 * - FID (First Input Delay) - 首次输入延迟
 * - FCP (First Contentful Paint) - 首次内容绘制
 * - LCP (Largest Contentful Paint) - 最大内容绘制
 * - TTFB (Time to First Byte) - 首字节时间
 * - FPS (Frames Per Second) - 帧率
 */

import { onCLS, onFCP, onLCP, onTTFB, type Metric } from 'web-vitals'

/**
 * 性能指标接口
 */
export interface PerformanceMetric {
  /** 指标名称 */
  name: string
  /** 指标值 */
  value: number
  /** 评级 (good | needs-improvement | poor) */
  rating: 'good' | 'needs-improvement' | 'poor'
  /** 时间戳 */
  timestamp: number
}

/**
 * 性指标阈值
 */
const THRESHOLDS = {
  LCP: { good: 2500, poor: 4000 },
  FID: { good: 100, poor: 300 },
  CLS: { good: 0.1, poor: 0.25 },
  FCP: { good: 1800, poor: 3000 },
  TTFB: { good: 800, poor: 1800 },
}

/**
 * 获取指标评级
 */
function getRating(name: string, value: number): 'good' | 'needs-improvement' | 'poor' {
  const threshold = (THRESHOLDS as any)[name]
  if (!threshold) return 'good'

  if (value <= threshold.good) return 'good'
  if (value <= threshold.poor) return 'needs-improvement'
  return 'poor'
}

/**
 * 日志处理函数
 */
type LogHandler = (metric: PerformanceMetric) => void

/**
 * 报告性能指标到控制台
 */
function logToConsole(metric: PerformanceMetric) {
  const emoji = {
    good: '✅',
    'needs-improvement': '⚠️',
    poor: '❌',
  }

  console.log(
    `${emoji[metric.rating]} [Performance] ${metric.name}:`,
    `${metric.value.toFixed(2)} ${metric.rating}`,
  )
}

/**
 * 性能监控类
 */
export class PerformanceMonitor {
  private metrics: Map<string, PerformanceMetric> = new Map()
  private logHandlers: LogHandler[] = []

  /**
   * 添加日志处理器
   */
  addLogHandler(handler: LogHandler) {
    this.logHandlers.push(handler)
  }

  /**
   * 移除日志处理器
   */
  removeLogHandler(handler: LogHandler) {
    const index = this.logHandlers.indexOf(handler)
    if (index > -1) {
      this.logHandlers.splice(index, 1)
    }
  }

  /**
   * 处理指标
   */
  private handleMetric(metric: Metric) {
    const performanceMetric: PerformanceMetric = {
      name: metric.name,
      value: metric.value,
      rating: getRating(metric.name, metric.value),
      timestamp: Date.now(),
    }

    // 保存指标
    this.metrics.set(metric.name, performanceMetric)

    // 调用所有日志处理器
    this.logHandlers.forEach((handler) => handler(performanceMetric))
  }

  /**
   * 启动性能监控
   */
  start() {
    // CLS - 布局偏移
    onCLS((metric) => this.handleMetric(metric))

    // FCP - 首次内容绘制
    onFCP((metric) => this.handleMetric(metric))

    // LCP - 最大内容绘制
    onLCP((metric) => this.handleMetric(metric))

    // TTFB - 首字节时间
    onTTFB((metric) => this.handleMetric(metric))
  }

  /**
   * 获取所有指标
   */
  getMetrics(): PerformanceMetric[] {
    return Array.from(this.metrics.values())
  }

  /**
   * 获取单个指标
   */
  getMetric(name: string): PerformanceMetric | undefined {
    return this.metrics.get(name)
  }

  /**
   * 生成性能报告
   */
  getReport(): string {
    const metrics = this.getMetrics()
    if (metrics.length === 0) {
      return '暂无性能数据'
    }

    let report = '\n📊 性能报告\n'

    metrics.forEach((metric) => {
      const emoji = {
        good: '✅',
        'needs-improvement': '⚠️',
        poor: '❌',
      }

      report += `\n${emoji[metric.rating]} ${metric.name}: ${metric.value.toFixed(2)}`
    })

    // 计算总体评分
    const goodCount = metrics.filter((m) => m.rating === 'good').length
    const totalCount = metrics.length
    const score = Math.round((goodCount / totalCount) * 100)

    report += `\n\n总体评分: ${score}%`

    return report
  }
}

/**
 * 全局性能监控实例
 */
let globalMonitor: PerformanceMonitor | null = null

/**
 * 初始化性能监控
 */
export function initPerformanceMonitoring() {
  if (typeof window === 'undefined') return

  if (!globalMonitor) {
    globalMonitor = new PerformanceMonitor()

    // 添加控制台日志
    globalMonitor.addLogHandler(logToConsole)

    // 启动监控
    globalMonitor.start()

    // 页面加载完成后显示报告
    if (document.readyState === 'complete') {
      setTimeout(() => {
        console.log(globalMonitor!.getReport())
      }, 0)
    } else {
      window.addEventListener('load', () => {
        setTimeout(() => {
          console.log(globalMonitor!.getReport())
        }, 0)
      })
    }
  }

  return globalMonitor
}

/**
 * 获取性能监控实例
 */
export function getPerformanceMonitor(): PerformanceMonitor | null {
  return globalMonitor
}

/**
 * FPS 监控类
 */
export class FPSMonitor {
  private fps: number[] = []
  private lastTime = performance.now()
  private frames = 0
  private rafId: number | null = null

  /**
   * 开始监控 FPS
   */
  start() {
    if (typeof window === 'undefined') return

    const measure = () => {
      const now = performance.now()
      this.frames++

      if (now >= this.lastTime + 1000) {
        const fps = Math.round((this.frames * 1000) / (now - this.lastTime))
        this.fps.push(fps)

        // 只保留最近 10 秒的数据
        if (this.fps.length > 10) {
          this.fps.shift()
        }

        this.frames = 0
        this.lastTime = now
      }

      this.rafId = requestAnimationFrame(measure)
    }

    this.rafId = requestAnimationFrame(measure)
  }

  /**
   * 停止监控
   */
  stop() {
    if (this.rafId !== null) {
      cancelAnimationFrame(this.rafId)
      this.rafId = null
    }
  }

  /**
   * 获取当前 FPS
   */
  getCurrentFPS(): number {
    return this.fps[this.fps.length - 1] || 0
  }

  /**
   * 获取平均 FPS
   */
  getAverageFPS(): number {
    if (this.fps.length === 0) return 0
    const sum = this.fps.reduce((a, b) => a + b, 0)
    return Math.round(sum / this.fps.length)
  }

  /**
   * 获取最低 FPS
   */
  getMinFPS(): number {
    if (this.fps.length === 0) return 0
    return Math.min(...this.fps)
  }

  /**
   * 获取 FPS 报告
   */
  getReport(): string {
    return `\n🎮 FPS 报告\n当前: ${this.getCurrentFPS()} FPS\n平均: ${this.getAverageFPS()} FPS\n最低: ${this.getMinFPS()} FPS`
  }
}

/**
 * 全局 FPS 监控实例
 */
let globalFPSMonitor: FPSMonitor | null = null

/**
 * 初始化 FPS 监控
 */
export function initFPSMonitoring() {
  if (typeof window === 'undefined') return

  if (!globalFPSMonitor) {
    globalFPSMonitor = new FPSMonitor()
    globalFPSMonitor.start()
  }

  return globalFPSMonitor
}

/**
 * 获取 FPS 监控实例
 */
export function getFPSMonitor(): FPSMonitor | null {
  return globalFPSMonitor
}

/**
 * 测量函数执行时间
 */
export function measureAsync<T>(name: string, fn: () => Promise<T>): Promise<T> {
  const start = performance.now()

  return fn().finally(() => {
    const duration = performance.now() - start
    console.log(`⏱️ [${name}] 执行时间: ${duration.toFixed(2)}ms`)
  })
}

/**
 * 测量同步函数执行时间
 */
export function measure<T>(name: string, fn: () => T): T {
  const start = performance.now()
  const result = fn()
  const duration = performance.now() - start

  console.log(`⏱️ [${name}] 执行时间: ${duration.toFixed(2)}ms`)

  return result
}

/**
 * 性能标记
 */
export function mark(name: string) {
  if (typeof window === 'undefined') return
  performance.mark(name)
}

/**
 * 测量两个标记之间的时间
 */
export function measureBetween(name: string, startMark: string, endMark: string) {
  if (typeof window === 'undefined') return

  try {
    performance.measure(name, startMark, endMark)
    const measure = performance.getEntriesByName(name)[0]
    console.log(`⏱️ [${name}] ${measure.duration.toFixed(2)}ms`)
  } catch (error) {
    console.warn(`无法测量 ${name}:`, error)
  }
}
