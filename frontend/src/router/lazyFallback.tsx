/**
 * 懒加载 fallback（router.tsx 与 lazyRoute.tsx 共用）。
 *
 * 独立叶子模块：两侧装配的依赖图约束不同（lazyRoute 测试不拖 router.tsx
 * 全量静态依赖，见该文件头注），共享件必须零第三方依赖只引 react。
 *
 * 居中形态（与应用其余加载态同语言）：顶替的 ChatContainer 自带会话头避让
 * （--safe-area-top 内边距），挂起期间 fallback 是聊天区顶元素——移动端全屏
 * 沉浸下顶部对齐会先后撞状态栏带与左上悬浮展开键（两者都在安全区正下方），
 * 居中即全部避开；桌面变量恒 0 不变。min-h 兜底父高不定时不塌成零高。
 */
export const LazyFallback = (
  <div
    className="text-muted-foreground flex h-full min-h-40 w-full items-center justify-center p-4"
    style={{ paddingTop: 'calc(2rem + var(--safe-area-top, 0px))' }}
  >
    加载中...
  </div>
)
