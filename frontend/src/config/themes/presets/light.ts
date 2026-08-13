/**
 * 浅色主题配置（默认亮色）
 *
 * 由 Deep Space v2 暗色主题反转得到：
 * - 保留品牌青 / AI 紫 / 辅助蓝（略加深以保证浅底对比度）
 * - 背景：深空蓝阶 → 冷灰蓝浅阶
 * - 文本：浅 slate → 深 slate
 * - 状态色：略加深，满足浅底可读性
 */

import type { ThemeConfig } from '@/types/theme'

export const lightTheme: ThemeConfig = {
  id: 'light',
  name: '浅色主题',
  description: 'Deep Space v2 反转亮色（默认浅色）',
  category: 'light',

  colors: {
    // 品牌色略加深，浅底上仍保持 Deep Space 识别度
    primary: '#0891B2',
    secondary: '#2563EB',
    accent: '#7C3AED',

    background: {
      main: '#F4F7FB',
      card: '#FFFFFF',
      sidebar: '#EEF2F8',
      input: '#E8EEF7',
      elevated: '#FFFFFF',
    },

    text: {
      primary: '#0B1220',
      secondary: '#334155',
      muted: '#64748B',
      disabled: '#94A3B8',
    },

    border: {
      default: 'rgba(15, 23, 42, 0.10)',
      hover: 'rgba(15, 23, 42, 0.18)',
      active: 'rgba(8, 145, 178, 0.50)',
    },

    status: {
      success: '#059669',
      warning: '#D97706',
      error: '#DC2626',
      info: '#2563EB',
      running: '#0891B2',
      pending: '#64748B',
    },

    bubble: {
      user_bg: '#0891B2',
      user_text: '#F8FAFC',
      user_radius: '1rem 1rem 1rem 0.25rem',
      user_shadow: '0 2px 8px rgba(8, 145, 178, 0.18)',
      ai_bg: '#EEF2F8',
      ai_text: '#0B1220',
      ai_radius: '1rem 1rem 1rem 0.25rem',
      ai_shadow: '0 1px 4px rgba(15, 23, 42, 0.06)',
      ai_border: '1px solid rgba(15, 23, 42, 0.08)',
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
        sm: '0 1px 2px 0 rgb(15 23 42 / 0.04)',
        md: '0 4px 6px -1px rgb(15 23 42 / 0.06)',
        lg: '0 10px 15px -3px rgb(15 23 42 / 0.08)',
      },
      normal: {
        sm: '0 1px 2px 0 rgb(15 23 42 / 0.06)',
        md: '0 4px 6px -1px rgb(15 23 42 / 0.08)',
        lg: '0 10px 15px -3px rgb(15 23 42 / 0.1)',
      },
      strong: {
        sm: '0 1px 3px 0 rgb(15 23 42 / 0.08)',
        md: '0 4px 8px -1px rgb(15 23 42 / 0.12)',
        lg: '0 10px 20px -3px rgb(15 23 42 / 0.14)',
      },
      defaultShadow: 'normal',
    },

    glow: {
      running: '0 0 12px rgba(8, 145, 178, 0.2)',
      waiting: '0 0 12px rgba(217, 119, 6, 0.18)',
      success: '0 0 12px rgba(5, 150, 105, 0.18)',
      error: '0 0 12px rgba(220, 38, 38, 0.18)',
      defaultGlowIntensity: 20,
    },

    button: {
      style: 'rounded',
      shadow: true,
      borderWidth: '1px',
      hoverEffect: 'lift',
      texture: 'none',
      textureOpacity: 0,
      variants: {
        primary: {
          bg: '#0891B2',
          text: '#F8FAFC',
          border: 'transparent',
          hoverBg: '#0E7490',
        },
        secondary: {
          bg: 'rgba(8, 145, 178, 0.10)',
          text: '#0E7490',
          border: 'rgba(8, 145, 178, 0.28)',
          hoverBg: 'rgba(8, 145, 178, 0.16)',
        },
        ghost: {
          bg: 'transparent',
          text: '#334155',
          border: 'transparent',
          hoverBg: 'rgba(15, 23, 42, 0.05)',
        },
        destructive: {
          bg: 'rgba(220, 38, 38, 0.1)',
          text: '#B91C1C',
          border: 'rgba(220, 38, 38, 0.28)',
          hoverBg: 'rgba(220, 38, 38, 0.16)',
        },
      },
    },

    input: {
      style: 'filled',
      focusBorder: '#0891B2',
      focusGlow: '0 0 0 3px rgba(8, 145, 178, 0.15)',
    },

    card: {
      style: 'elevated',
      blur: '0',
      border: '1px solid rgba(15, 23, 42, 0.10)',
    },

    badge: {
      borderRadius: '9999px',
      variants: {
        default: {
          bg: '#0891B2',
          text: '#F8FAFC',
          border: 'transparent',
        },
        secondary: {
          bg: 'rgba(15, 23, 42, 0.06)',
          text: '#334155',
          border: 'transparent',
        },
        success: {
          bg: 'rgba(5, 150, 105, 0.12)',
          text: '#047857',
          border: 'rgba(5, 150, 105, 0.28)',
        },
        warning: {
          bg: 'rgba(217, 119, 6, 0.12)',
          text: '#B45309',
          border: 'rgba(217, 119, 6, 0.28)',
        },
        error: {
          bg: 'rgba(220, 38, 38, 0.12)',
          text: '#B91C1C',
          border: 'rgba(220, 38, 38, 0.28)',
        },
        info: {
          bg: 'rgba(37, 99, 235, 0.12)',
          text: '#1D4ED8',
          border: 'rgba(37, 99, 235, 0.28)',
        },
      },
    },

    dialog: {
      borderRadius: '0.75rem',
      overlayBg: '#0B1220',
      overlayOpacity: 0.45,
      shadow: '0 25px 50px -12px rgba(15, 23, 42, 0.2)',
      border: '1px solid rgba(15, 23, 42, 0.1)',
    },

    tabs: {
      borderRadius: '0.5rem',
      listBg: 'rgba(15, 23, 42, 0.05)',
      activeBg: '#FFFFFF',
      activeText: '#0891B2',
      inactiveText: '#64748B',
    },

    toast: {
      borderRadius: '0.625rem',
      shadow: '0 10px 15px -3px rgba(15, 23, 42, 0.1)',
      variants: {
        default: {
          bg: '#FFFFFF',
          text: '#0B1220',
          border: 'rgba(15, 23, 42, 0.1)',
        },
        success: {
          bg: 'rgba(5, 150, 105, 0.1)',
          text: '#047857',
          border: 'rgba(5, 150, 105, 0.28)',
        },
        error: {
          bg: 'rgba(220, 38, 38, 0.1)',
          text: '#B91C1C',
          border: 'rgba(220, 38, 38, 0.28)',
        },
        warning: {
          bg: 'rgba(217, 119, 6, 0.1)',
          text: '#B45309',
          border: 'rgba(217, 119, 6, 0.28)',
        },
        info: {
          bg: 'rgba(37, 99, 235, 0.1)',
          text: '#1D4ED8',
          border: 'rgba(37, 99, 235, 0.28)',
        },
      },
    },

    progress: {
      borderRadius: '0.125rem',
      trackBg: '#E2E8F0',
      variants: {
        default: '#0891B2',
        success: '#059669',
        warning: '#D97706',
        error: '#DC2626',
      },
    },

    dropdownMenu: {
      borderRadius: '0.625rem',
      shadow: '0 10px 15px -3px rgba(15, 23, 42, 0.1)',
      border: '1px solid rgba(15, 23, 42, 0.1)',
      itemHoverBg: 'rgba(15, 23, 42, 0.05)',
      itemHoverText: '#0B1220',
    },
  },

  effects: {
    glassmorphism: false,
    animations: true,
    transitionDuration: 200,
    transitionEasing: 'cubic-bezier(0.32, 0, 0.67, 0)',
  },

  backgrounds: {
    main: {
      type: 'solid',
      value: '#F4F7FB',
    },
    image: {
      enabled: false,
      url: '',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(244, 247, 251, 0.9)',
      overlayOpacity: 0.9,
    },
    texture: {
      type: 'none',
      color: 'rgba(15, 23, 42, 0.03)',
      size: '24px',
      opacity: 0.05,
    },
    sidebar: {
      type: 'solid',
      value: '#EEF2F8',
      texture: { type: 'none' },
    },
    chat: {
      type: 'solid',
      value: '#F4F7FB',
    },
  },
}
