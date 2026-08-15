/*
 * Ported from DeepSeek Harness (DSH) — MIT License.
 * Copyright (c) 2026 DeepSeek. Source: packages/client/ui-primitives/src/Pill.tsx
 * Repo pinned at commit 47f943859bef60e4160492346772ded9b24f765a (version 0.1.0-rc.5). Adapted for AgentOS
 * (task_dsh_plugin_adapter 任务 3): verbatim copy.
 */
// Pill: small rounded label chip (view switcher tabs, filters, badges).

import type { ButtonHTMLAttributes, ReactNode } from 'react'
import clsx from 'clsx'
import css from './Pill.module.css'

/**
 * Render a pill chip. Interactive when onClick is supplied (renders a button);
 * otherwise a static span.
 * @param props.active - selected/active visual state.
 * @returns pill element.
 */
export function Pill({ active = false, className, children, onClick, ...rest }: {
  active?: boolean
  // `| undefined` so a caller can forward an optional class straight through
  // under exactOptionalPropertyTypes (a CSS-module lookup is string|undefined).
  className?: string | undefined
  children?: ReactNode
} & ButtonHTMLAttributes<HTMLButtonElement>) {
  if (!onClick) {
    return <span className={clsx(css.pill, active && css.active, className)}>{children}</span>
  }
  return (
    <button
      type="button"
      className={clsx(css.pill, css.interactive, active && css.active, className)}
      onClick={onClick}
      {...rest}
    >
      {children}
    </button>
  )
}
