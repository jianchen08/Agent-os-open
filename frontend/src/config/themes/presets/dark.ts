/**
 * 深色主题配置
 *
 * 默认深色主题，适合夜间使用
 */

import type { ThemeConfig } from '@/types/theme'

export const darkTheme: ThemeConfig = {
  id: 'dark',
  name: '深色主题',
  description: '默认深色主题，适合夜间使用',
  category: 'dark',

  colors: {
    // 基于 Radix slate-dark 中性层 + indigo-dark 强调：多层灰造深度，避免纯黑刺眼
    primary: '#3e63dd',
    secondary: '#435db1',
    accent: '#9eb1ff',

    background: {
      main: '#111113',
      card: '#18191b',
      sidebar: '#18191b',
      input: '#272a2d',
      elevated: '#212225',
    },

    text: {
      primary: '#edeef0',
      secondary: '#b0b4ba',
      muted: '#777b84',
      disabled: '#5a6169',
    },

    border: {
      default: 'rgba(255, 255, 255, 0.10)',
      hover: 'rgba(255, 255, 255, 0.18)',
      active: 'rgba(62, 99, 221, 0.55)',
    },

    status: {
      success: '#0bd8b6',
      warning: '#ffc53d',
      error: '#ff6b6b',
      info: '#9eb1ff',
      running: '#4ccce6',
      pending: '#777b84',
    },

    bubble: {
      user_bg: '#3e63dd',
      user_text: '#ffffff',
      user_radius: '1rem 1rem 1rem 0.25rem',
      user_shadow: '0 4px 12px rgba(62, 99, 221, 0.28)',
      ai_bg: '#212225',
      ai_text: '#edeef0',
      ai_radius: '0.875rem 0.875rem 0.875rem 0.25rem',
      ai_shadow: '0 2px 8px rgba(0, 0, 0, 0.25)',
      ai_border: '1px solid rgba(255, 255, 255, 0.08)',
    },
  },

  components: {
    borderRadius: {
      none: '0',
      sm: '0.25rem',
      md: '0.5rem',
      lg: '0.75rem',
      xl: '1rem',
      full: '9999px',
      defaultRadius: 'md',
    },

    fonts: {
      ui: "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
      code: "'JetBrains Mono', 'Fira Code', 'Consolas', monospace",
    },

    fontSize: {
      xs: '14px',
      sm: '15px',
      md: '16px',
      lg: '17px',
      xl: '18px',
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
      running: '0 0 15px rgba(0, 240, 255, 0.3)',
      waiting: '0 0 15px rgba(245, 158, 11, 0.3)',
      success: '0 0 15px rgba(16, 185, 129, 0.3)',
      error: '0 0 15px rgba(239, 68, 68, 0.3)',
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
          bg: '#3e63dd',
          text: '#ffffff',
          border: 'transparent',
          hoverBg: '#5472e4',
        },
        secondary: {
          bg: 'rgba(158, 177, 255, 0.12)',
          text: '#9eb1ff',
          border: 'rgba(158, 177, 255, 0.25)',
          hoverBg: 'rgba(158, 177, 255, 0.18)',
        },
        ghost: {
          bg: 'transparent',
          text: '#b0b4ba',
          border: 'transparent',
          hoverBg: 'rgba(255, 255, 255, 0.06)',
        },
        destructive: {
          bg: 'rgba(255, 107, 107, 0.15)',
          text: '#ff9b9b',
          border: 'rgba(255, 107, 107, 0.35)',
          hoverBg: 'rgba(255, 107, 107, 0.25)',
        },
      },
    },

    input: {
      style: 'filled',
      focusBorder: '#3b82f6',
      focusGlow: '0 0 0 3px rgba(59, 130, 246, 0.2)',
    },

    card: {
      style: 'glass',
      blur: '12px',
      border: '1px solid rgba(255, 255, 255, 0.08)',
    },

    badge: {
      borderRadius: '9999px',
      variants: {
        default: {
          bg: '#3b82f6',
          text: '#ffffff',
          border: 'transparent',
        },
        secondary: {
          bg: 'rgba(255,255,255,0.1)',
          text: '#f8fafc',
          border: 'transparent',
        },
        success: {
          bg: 'rgba(16, 185, 129, 0.25)',
          text: '#6ee7b7',
          border: 'rgba(16, 185, 129, 0.5)',
        },
        warning: {
          bg: 'rgba(245, 158, 11, 0.25)',
          text: '#fcd34d',
          border: 'rgba(245, 158, 11, 0.5)',
        },
        error: {
          bg: 'rgba(239, 68, 68, 0.25)',
          text: '#fca5a5',
          border: 'rgba(239, 68, 68, 0.5)',
        },
        info: {
          bg: 'rgba(59, 130, 246, 0.25)',
          text: '#93c5fd',
          border: 'rgba(59, 130, 246, 0.5)',
        },
      },
    },

    dialog: {
      borderRadius: '1rem',
      overlayBg: '#000000',
      overlayOpacity: 0.8,
      shadow: '0 25px 50px -12px rgba(0, 0, 0, 0.5)',
      border: '1px solid rgba(255, 255, 255, 0.1)',
    },

    tabs: {
      borderRadius: '0.5rem',
      listBg: 'rgba(255, 255, 255, 0.05)',
      activeBg: '#1e293b',
      activeText: '#f8fafc',
      inactiveText: '#94a3b8',
    },

    toast: {
      borderRadius: '0.75rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.4)',
      variants: {
        default: {
          bg: '#1e293b',
          text: '#f8fafc',
          border: 'rgba(255, 255, 255, 0.1)',
        },
        success: {
          bg: 'rgba(16, 185, 129, 0.2)',
          text: '#6ee7b7',
          border: 'rgba(16, 185, 129, 0.5)',
        },
        error: {
          bg: 'rgba(239, 68, 68, 0.2)',
          text: '#fca5a5',
          border: 'rgba(239, 68, 68, 0.5)',
        },
        warning: {
          bg: 'rgba(245, 158, 11, 0.2)',
          text: '#fcd34d',
          border: 'rgba(245, 158, 11, 0.5)',
        },
        info: {
          bg: 'rgba(59, 130, 246, 0.2)',
          text: '#93c5fd',
          border: 'rgba(59, 130, 246, 0.5)',
        },
      },
    },

    progress: {
      borderRadius: '9999px',
      trackBg: 'rgba(59, 130, 246, 0.2)',
      variants: {
        default: '#3b82f6',
        success: '#10b981',
        warning: '#f59e0b',
        error: '#ef4444',
      },
    },

    dropdownMenu: {
      borderRadius: '0.75rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.4)',
      border: '1px solid rgba(255, 255, 255, 0.1)',
      itemHoverBg: 'rgba(255, 255, 255, 0.1)',
      itemHoverText: '#f8fafc',
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
      type: 'gradient',
      value: 'radial-gradient(circle at 50% 0%, #0f172a, #020617)',
    },
    image: {
      enabled: false,
      url: '',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(15, 23, 42, 0.85)',
      overlayOpacity: 0.85,
    },
    texture: {
      type: 'grid',
      color: 'rgba(255, 255, 255, 0.03)',
      size: '24px',
      opacity: 0.1,
    },
    sidebar: {
      type: 'solid',
      value: '#1e293b',
      texture: { type: 'none' },
    },
    chat: {
      type: 'gradient',
      value: 'linear-gradient(180deg, #0f172a 0%, #1e293b 100%)',
    },
  },
}
