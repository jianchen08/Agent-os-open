/**
 * 深色主题配置（默认主题）
 *
 * 对齐设计稿 A 区 · 默认主题规范 Deep Space v2
 * 来源：https://ardot.tencent.com/file/707091497079378
 */

import type { ThemeConfig } from '@/types/theme'

export const darkTheme: ThemeConfig = {
  id: 'dark',
  name: '深色主题',
  description: 'Deep Space v2 默认深色主题',
  category: 'dark',

  colors: {
    // Deep Space v2：克制深空蓝底 + 品牌青点缀
    primary: '#22D3EE',
    secondary: '#3B82F6',
    accent: '#A78BFA',

    background: {
      main: '#04060F',
      card: '#0A1226',
      sidebar: '#0A1226',
      input: '#111C38',
      elevated: '#111C38',
    },

    text: {
      primary: '#F1F5F9',
      secondary: '#CBD5E1',
      muted: '#94A3B8',
      disabled: '#64748B',
    },

    border: {
      default: 'rgba(148, 163, 184, 0.12)',
      hover: 'rgba(148, 163, 184, 0.22)',
      active: 'rgba(34, 211, 238, 0.45)',
    },

    status: {
      success: '#34D399',
      warning: '#FBBF24',
      error: '#F87171',
      info: '#60A5FA',
      running: '#22D3EE',
      pending: '#94A3B8',
    },

    bubble: {
      user_bg: '#22D3EE',
      user_text: '#04060F',
      user_radius: '1rem 1rem 1rem 0.25rem',
      user_shadow: '0 2px 8px rgba(34, 211, 238, 0.2)',
      ai_bg: '#111C38',
      ai_text: '#F1F5F9',
      ai_radius: '1rem 1rem 1rem 0.25rem',
      ai_shadow: '0 2px 8px rgba(0, 0, 0, 0.25)',
      ai_border: '1px solid rgba(148, 163, 184, 0.12)',
    },
  },

  components: {
    borderRadius: {
      none: '0',
      sm: '0.375rem',
      md: '0.5rem',
      lg: '0.625rem',
      xl: '0.75rem',
      full: '9999px',
      defaultRadius: 'md',
    },

    fonts: {
      ui: "'Noto Sans SC', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      code: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
    },

    fontSize: {
      // 对齐 Tailwind 默认阶梯：字号变量接管后默认主题观感不变
      xs: '12px',
      sm: '14px',
      md: '16px',
      lg: '18px',
      xl: '20px',
      defaultFontSize: 'md',
    },

    shadows: {
      none: { sm: 'none', md: 'none', lg: 'none' },
      light: {
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.2)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.25)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.25)',
      },
      normal: {
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.3)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.4)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.4)',
      },
      strong: {
        sm: '0 1px 3px 0 rgb(0 0 0 / 0.4)',
        md: '0 4px 8px -1px rgb(0 0 0 / 0.5)',
        lg: '0 10px 20px -3px rgb(0 0 0 / 0.5)',
      },
      defaultShadow: 'normal',
    },

    glow: {
      running: '0 0 15px rgba(34, 211, 238, 0.3)',
      waiting: '0 0 15px rgba(251, 191, 36, 0.3)',
      success: '0 0 15px rgba(52, 211, 153, 0.3)',
      error: '0 0 15px rgba(248, 113, 113, 0.3)',
      defaultGlowIntensity: 30,
    },

    button: {
      style: 'rounded',
      shadow: true,
      borderWidth: '1px',
      hoverEffect: 'lift',
      texture: 'glass',
      textureOpacity: 0.1,
      variants: {
        primary: {
          bg: '#22D3EE',
          text: '#04060F',
          border: 'transparent',
          hoverBg: '#4ADFF2',
        },
        secondary: {
          bg: 'rgba(34, 211, 238, 0.12)',
          text: '#22D3EE',
          border: 'rgba(34, 211, 238, 0.35)',
          hoverBg: 'rgba(34, 211, 238, 0.2)',
        },
        ghost: {
          bg: 'transparent',
          text: '#CBD5E1',
          border: 'transparent',
          hoverBg: 'rgba(148, 163, 184, 0.1)',
        },
        destructive: {
          bg: 'rgba(248, 113, 113, 0.15)',
          text: '#FCA5A5',
          border: 'rgba(248, 113, 113, 0.35)',
          hoverBg: 'rgba(248, 113, 113, 0.25)',
        },
      },
    },

    input: {
      style: 'filled',
      focusBorder: '#22D3EE',
      focusGlow: '0 0 0 3px rgba(34, 211, 238, 0.2)',
    },

    card: {
      style: 'glass',
      blur: '12px',
      border: '1px solid rgba(148, 163, 184, 0.12)',
    },

    badge: {
      borderRadius: '9999px',
      variants: {
        default: {
          bg: '#22D3EE',
          text: '#04060F',
          border: 'transparent',
        },
        secondary: {
          bg: 'rgba(148, 163, 184, 0.12)',
          text: '#CBD5E1',
          border: 'transparent',
        },
        success: {
          bg: 'rgba(52, 211, 153, 0.18)',
          text: '#6EE7B7',
          border: 'rgba(52, 211, 153, 0.45)',
        },
        warning: {
          bg: 'rgba(251, 191, 36, 0.18)',
          text: '#FCD34D',
          border: 'rgba(251, 191, 36, 0.45)',
        },
        error: {
          bg: 'rgba(248, 113, 113, 0.18)',
          text: '#FCA5A5',
          border: 'rgba(248, 113, 113, 0.45)',
        },
        info: {
          bg: 'rgba(96, 165, 250, 0.18)',
          text: '#93C5FD',
          border: 'rgba(96, 165, 250, 0.45)',
        },
      },
    },

    dialog: {
      borderRadius: '0.75rem',
      overlayBg: '#000000',
      overlayOpacity: 0.8,
      shadow: '0 25px 50px -12px rgba(0, 0, 0, 0.5)',
      border: '1px solid rgba(148, 163, 184, 0.15)',
    },

    tabs: {
      borderRadius: '0.5rem',
      listBg: 'rgba(148, 163, 184, 0.08)',
      activeBg: '#111C38',
      activeText: '#22D3EE',
      inactiveText: '#94A3B8',
    },

    toast: {
      borderRadius: '0.625rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.4)',
      variants: {
        default: {
          bg: '#111C38',
          text: '#F1F5F9',
          border: 'rgba(148, 163, 184, 0.15)',
        },
        success: {
          bg: 'rgba(52, 211, 153, 0.2)',
          text: '#6EE7B7',
          border: 'rgba(52, 211, 153, 0.5)',
        },
        error: {
          bg: 'rgba(248, 113, 113, 0.2)',
          text: '#FCA5A5',
          border: 'rgba(248, 113, 113, 0.5)',
        },
        warning: {
          bg: 'rgba(251, 191, 36, 0.2)',
          text: '#FCD34D',
          border: 'rgba(251, 191, 36, 0.5)',
        },
        info: {
          bg: 'rgba(96, 165, 250, 0.2)',
          text: '#93C5FD',
          border: 'rgba(96, 165, 250, 0.5)',
        },
      },
    },

    progress: {
      borderRadius: '0.125rem',
      trackBg: '#121C38',
      variants: {
        default: '#22D3EE',
        success: '#34D399',
        warning: '#FBBF24',
        error: '#F87171',
      },
    },

    dropdownMenu: {
      borderRadius: '0.625rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.4)',
      border: '1px solid rgba(148, 163, 184, 0.15)',
      itemHoverBg: 'rgba(148, 163, 184, 0.1)',
      itemHoverText: '#F1F5F9',
    },
  },

  effects: {
    glassmorphism: true,
    animations: true,
    transitionDuration: 200,
    transitionEasing: 'cubic-bezier(0.32, 0, 0.67, 0)',
  },

  backgrounds: {
    main: {
      type: 'solid',
      value: '#04060F',
    },
    image: {
      enabled: false,
      url: '',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(4, 6, 15, 0.85)',
      overlayOpacity: 0.85,
    },
    texture: {
      type: 'none',
      color: 'rgba(255, 255, 255, 0.03)',
      size: '24px',
      opacity: 0.1,
    },
    sidebar: {
      type: 'solid',
      value: '#0A1226',
      texture: { type: 'none' },
    },
    chat: {
      type: 'solid',
      value: '#04060F',
    },
  },
}
