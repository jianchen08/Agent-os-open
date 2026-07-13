/**
 * 浅色主题配置
 *
 * 默认浅色主题，适合日间使用
 */

import type { ThemeConfig } from '@/types/theme'

export const lightTheme: ThemeConfig = {
  id: 'light',
  name: '浅色主题',
  description: '默认浅色主题，适合日间使用',
  category: 'light',

  colors: {
    // 基于 Radix slate 浅中性 + 同色系蓝：克制干净，层次由浅到深
    primary: '#3e63dd',
    secondary: '#5472e4',
    accent: '#9eb1ff',

    background: {
      main: '#fcfcfd',
      card: '#ffffff',
      sidebar: '#f9f9fb',
      input: '#f0f0f3',
      elevated: '#ffffff',
    },

    text: {
      primary: '#1c2024',
      secondary: '#60646c',
      muted: '#80838d',
      disabled: '#8b8d98',
    },

    border: {
      default: 'rgba(0, 0, 0, 0.08)',
      hover: 'rgba(0, 0, 0, 0.14)',
      active: 'rgba(62, 99, 221, 0.45)',
    },

    status: {
      success: '#0d3d38',
      warning: '#a8650b',
      error: '#dc2626',
      info: '#3e63dd',
      running: '#00749e',
      pending: '#80838d',
    },

    bubble: {
      user_bg: '#3e63dd',
      user_text: '#ffffff',
      user_radius: '1rem 1rem 1rem 0.25rem',
      user_shadow: '0 4px 12px rgba(62, 99, 221, 0.22)',
      ai_bg: '#f0f0f3',
      ai_text: '#1c2024',
      ai_radius: '0.875rem 0.875rem 0.875rem 0.25rem',
      ai_shadow: '0 2px 8px rgba(0, 0, 0, 0.06)',
      ai_border: '1px solid rgba(0, 0, 0, 0.06)',
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
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.03)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.05)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.05)',
      },
      normal: {
        sm: '0 1px 2px 0 rgb(0 0 0 / 0.05)',
        md: '0 4px 6px -1px rgb(0 0 0 / 0.1)',
        lg: '0 10px 15px -3px rgb(0 0 0 / 0.1)',
      },
      strong: {
        sm: '0 1px 3px 0 rgb(0 0 0 / 0.1)',
        md: '0 4px 8px -1px rgb(0 0 0 / 0.15)',
        lg: '0 10px 20px -3px rgb(0 0 0 / 0.2)',
      },
      defaultShadow: 'normal',
    },

    glow: {
      running: '0 0 15px rgba(8, 145, 178, 0.2)',
      waiting: '0 0 15px rgba(217, 119, 6, 0.2)',
      success: '0 0 15px rgba(5, 150, 105, 0.2)',
      error: '0 0 15px rgba(220, 38, 38, 0.2)',
      defaultGlowIntensity: 20,
    },

    button: {
      style: 'rounded',
      shadow: true,
      borderWidth: '1px',
      hoverEffect: 'darken',
      texture: 'none',
      textureOpacity: 0,
      variants: {
        primary: {
          bg: '#3e63dd',
          text: '#ffffff',
          border: 'transparent',
          hoverBg: '#304384',
        },
        secondary: {
          bg: '#f0f0f3',
          text: '#1c2024',
          border: '#e0e1e6',
          hoverBg: '#e0e1e6',
        },
        ghost: {
          bg: 'transparent',
          text: '#60646c',
          border: 'transparent',
          hoverBg: 'rgba(0, 0, 0, 0.05)',
        },
        destructive: {
          bg: '#dc2626',
          text: '#ffffff',
          border: 'transparent',
          hoverBg: '#b91c1c',
        },
      },
    },

    input: {
      style: 'outlined',
      focusBorder: '#2563eb',
      focusGlow: '0 0 0 3px rgba(37, 99, 235, 0.1)',
    },

    card: {
      style: 'elevated',
      blur: '0',
      border: '1px solid rgba(0, 0, 0, 0.08)',
    },

    badge: {
      borderRadius: '9999px',
      variants: {
        default: {
          bg: '#2563eb',
          text: '#ffffff',
          border: 'transparent',
        },
        secondary: {
          bg: '#f1f5f9',
          text: '#0f172a',
          border: 'transparent',
        },
        success: {
          bg: '#dcfce7',
          text: '#166534',
          border: '#bbf7d0',
        },
        warning: {
          bg: '#fef3c7',
          text: '#92400e',
          border: '#fde68a',
        },
        error: {
          bg: '#fee2e2',
          text: '#991b1b',
          border: '#fecaca',
        },
        info: {
          bg: '#dbeafe',
          text: '#1e40af',
          border: '#bfdbfe',
        },
      },
    },

    dialog: {
      borderRadius: '1rem',
      overlayBg: '#000000',
      overlayOpacity: 0.5,
      shadow: '0 25px 50px -12px rgba(0, 0, 0, 0.25)',
      border: '1px solid rgba(0, 0, 0, 0.1)',
    },

    tabs: {
      borderRadius: '0.5rem',
      listBg: '#f1f5f9',
      activeBg: '#ffffff',
      activeText: '#0f172a',
      inactiveText: '#64748b',
    },

    toast: {
      borderRadius: '0.75rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.1)',
      variants: {
        default: {
          bg: '#ffffff',
          text: '#0f172a',
          border: 'rgba(0, 0, 0, 0.1)',
        },
        success: {
          bg: '#dcfce7',
          text: '#166534',
          border: '#bbf7d0',
        },
        error: {
          bg: '#fee2e2',
          text: '#991b1b',
          border: '#fecaca',
        },
        warning: {
          bg: '#fef3c7',
          text: '#92400e',
          border: '#fde68a',
        },
        info: {
          bg: '#dbeafe',
          text: '#1e40af',
          border: '#bfdbfe',
        },
      },
    },

    progress: {
      borderRadius: '9999px',
      trackBg: 'rgba(37, 99, 235, 0.2)',
      variants: {
        default: '#2563eb',
        success: '#059669',
        warning: '#d97706',
        error: '#dc2626',
      },
    },

    dropdownMenu: {
      borderRadius: '0.75rem',
      shadow: '0 10px 15px -3px rgba(0, 0, 0, 0.1)',
      border: '1px solid rgba(0, 0, 0, 0.1)',
      itemHoverBg: '#f1f5f9',
      itemHoverText: '#0f172a',
    },
  },

  effects: {
    glassmorphism: false,
    animations: true,
    transitionDuration: 200,
    transitionEasing: 'cubic-bezier(0.32, 0, 0.67, 0)',
  },

  backgrounds: {
    main: { type: 'solid', value: '#f8fafc' },
    image: {
      enabled: false,
      url: '',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(255, 255, 255, 0.9)',
      overlayOpacity: 0.9,
    },
    texture: {
      type: 'none',
      color: 'rgba(0, 0, 0, 0.02)',
      size: '24px',
      opacity: 0.05,
    },
    sidebar: { type: 'solid', value: '#ffffff', texture: { type: 'none' } },
    chat: { type: 'solid', value: '#f8fafc' },
  },
}
