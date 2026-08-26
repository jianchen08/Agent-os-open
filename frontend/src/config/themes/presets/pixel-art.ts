/**
 * 像素糖果主题
 *
 * 基于 PICO-8 官方 16 色板(像素艺术事实标准)的复古游戏机风:
 * - 暖白画布 + 浅杏侧边栏,高饱和亮色只做小面积点睛,不大面积铺色
 * - 深藏青(#1D2B53)实线描边 + 零模糊硬阴影:像素 UI 正统,中性打底、亮色点睛
 * - 像素点阵字体(本地 Fusion Pixel)+ 棋盘格纹理 + 阶跃动画
 */

import type { ThemeConfig } from '@/types/theme'

export const pixelArtTheme: ThemeConfig = {
  id: 'pixel-art',
  name: '像素糖果',
  description: 'PICO-8 复古色板:暖白画布+实线描边+积木硬阴影,藏青点睛,明快不刺眼',
  category: 'light',

  colors: {
    // PICO-8: Blue 主操作 / Pink 副色 / Yellow 点缀
    primary: '#29adff',
    secondary: '#ff77a8',
    accent: '#ffec27',

    background: {
      // PICO-8 White(暖屏幕白)做画布,卡片用纯白提亮;侧栏浅杏退为陪衬
      main: '#fff1e8',
      card: '#ffffff',
      sidebar: '#ffe3cc',
      input: '#ffffff',
      elevated: '#fff1e8',
    },

    text: {
      // PICO-8 Dark Blue 当"黑"(像素游戏惯例,比纯黑柔和),次级降透明出层次
      primary: '#1d2b53',
      secondary: 'rgba(29, 43, 83, 0.72)',
      muted: '#5f574f',
      disabled: '#7d7a82',
    },

    border: {
      default: 'rgba(29, 43, 83, 0.35)',
      hover: 'rgba(126, 37, 83, 0.55)',
      active: '#1d2b53',
    },

    status: {
      // PICO-8 亮色组:Green/Orange/Red/Blue
      success: '#007545',
      warning: '#8a6300',
      error: '#c81e4a',
      info: '#175fb0',
      running: '#175fb0',
      pending: '#5f574f',
    },

    bubble: {
      // 用户气泡走像素正统"实线描边泡"：暖白面 + 藏青描边 + 硬阴影，
      // 不用藏青大面积铺色（亮色点睛、中性打底）；气泡内页面级内容色
      // （链接 hsl(--primary)/行内码 hsl(--muted)）与全站同底同读
      user_bg: '#ffead8',
      user_text: '#1d2b53',
      user_radius: '2px',
      user_shadow: '3px 3px 0 rgba(29, 43, 83, 0.35)',
      user_border: '2px solid #1d2b53',
      ai_bg: '#ffffff',
      ai_text: '#1d2b53',
      ai_radius: '2px',
      ai_shadow: '3px 3px 0 rgba(29, 43, 83, 0.2)',
      ai_border: '2px solid #1d2b53',
    },
  },

  components: {
    // 全站边框线型:实线描边 = 像素 UI 正统(虚线全铺会碎、显乱)
    borderStyle: 'solid',

    borderRadius: {
      none: '0',
      sm: '0',
      md: '2px',
      lg: '4px',
      xl: '4px',
      full: '4px',
      defaultRadius: 'sm',
    },

    fonts: {
      // 本地字体(public/fonts/):Fusion Pixel 覆盖简中+拉丁,离线可用
      ui: "'Fusion Pixel 12px', 'Pixelify Sans', 'YouYuan', '幼圆', 'Microsoft YaHei', sans-serif",
      code: "'Press Start 2P', 'Fusion Pixel 12px', 'JetBrains Mono', monospace",
    },

    fontSize: {
      xs: '14px',
      sm: '15px',
      md: '16px',
      lg: '18px',
      xl: '20px',
      defaultFontSize: 'md',
    },

    shadows: {
      // 零模糊偏移投影 = 经典像素"积木块"硬阴影(PICO-8 深蓝)
      none: { sm: 'none', md: 'none', lg: 'none' },
      light: {
        sm: '2px 2px 0 rgba(29, 43, 83, 0.25)',
        md: '3px 3px 0 rgba(29, 43, 83, 0.3)',
        lg: '4px 4px 0 rgba(29, 43, 83, 0.35)',
      },
      normal: {
        sm: '2px 2px 0 rgba(29, 43, 83, 0.45)',
        md: '4px 4px 0 rgba(29, 43, 83, 0.5)',
        lg: '5px 5px 0 rgba(29, 43, 83, 0.55)',
      },
      strong: {
        sm: '3px 3px 0 rgba(29, 43, 83, 0.65)',
        md: '5px 5px 0 rgba(29, 43, 83, 0.7)',
        lg: '6px 6px 0 rgba(29, 43, 83, 0.75)',
      },
      defaultShadow: 'normal',
    },

    glow: {
      running: '0 0 0 transparent',
      waiting: '0 0 0 transparent',
      success: '0 0 0 transparent',
      error: '0 0 0 transparent',
      defaultGlowIntensity: 0,
    },

    button: {
      style: 'square',
      shadow: true,
      borderWidth: '2px',
      hoverEffect: 'lift',
      texture: 'none',
      textureOpacity: 0,
      variants: {
        primary: {
          bg: '#29adff',
          text: '#1d2b53',
          border: '#1d2b53',
          hoverBg: '#4dbcff',
        },
        secondary: {
          bg: '#ffec27',
          text: '#1d2b53',
          border: '#1d2b53',
          hoverBg: '#fff45c',
        },
        ghost: {
          bg: 'transparent',
          text: '#1d2b53',
          border: 'transparent',
          hoverBg: 'rgba(41, 173, 255, 0.15)',
        },
        destructive: {
          bg: '#d1204f',
          text: '#ffffff',
          border: '#1d2b53',
          hoverBg: '#ff3071',
        },
      },
    },

    input: {
      style: 'outlined',
      focusBorder: '#29adff',
      focusGlow: '0 0 0 2px #29adff',
    },

    card: {
      style: 'solid',
      blur: '0px',
      border: '2px solid #1d2b53',
    },

    badge: {
      borderRadius: '2px',
      variants: {
        default: { bg: '#29adff', text: '#1d2b53', border: '#1d2b53' },
        secondary: { bg: '#ffec27', text: '#1d2b53', border: '#1d2b53' },
        success: { bg: '#00e436', text: '#1d2b53', border: '#1d2b53' },
        warning: { bg: '#ffa300', text: '#1d2b53', border: '#1d2b53' },
        error: { bg: '#d1204f', text: '#ffffff', border: '#1d2b53' },
        info: { bg: '#29adff', text: '#1d2b53', border: '#1d2b53' },
      },
    },

    dialog: {
      borderRadius: '2px',
      overlayBg: '#1d2b53',
      overlayOpacity: 0.5,
      shadow: '6px 6px 0 rgba(29, 43, 83, 0.6)',
      border: '2px solid #1d2b53',
    },

    tabs: {
      borderRadius: '2px',
      listBg: 'rgba(29, 43, 83, 0.08)',
      activeBg: '#29adff',
      activeText: '#1d2b53',
      inactiveText: '#5f574f',
    },

    toast: {
      borderRadius: '2px',
      shadow: '4px 4px 0 rgba(29, 43, 83, 0.5)',
      variants: {
        default: { bg: '#ffffff', text: '#1d2b53', border: '2px solid #1d2b53' },
        success: { bg: '#00e436', text: '#1d2b53', border: '2px solid #1d2b53' },
        error: { bg: '#d1204f', text: '#ffffff', border: '2px solid #1d2b53' },
        warning: { bg: '#ffa300', text: '#1d2b53', border: '2px solid #1d2b53' },
        info: { bg: '#29adff', text: '#1d2b53', border: '2px solid #1d2b53' },
      },
    },

    progress: {
      borderRadius: '2px',
      trackBg: 'rgba(29, 43, 83, 0.15)',
      variants: {
        default: '#29adff',
        success: '#00e436',
        warning: '#ffa300',
        error: '#ff004d',
      },
    },

    dropdownMenu: {
      borderRadius: '2px',
      shadow: '4px 4px 0 rgba(29, 43, 83, 0.5)',
      border: '2px solid #1d2b53',
      itemHoverBg: '#29adff',
      itemHoverText: '#1d2b53',
    },
  },

  effects: {
    glassmorphism: false,
    animations: true,
    transitionDuration: 120,
    transitionEasing: 'steps(4, end)',
  },

  backgrounds: {
    main: {
      type: 'solid',
      value: '#fff1e8',
    },
    image: {
      enabled: false,
      url: '',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(255, 241, 232, 0.5)',
      overlayOpacity: 0.5,
    },
    texture: {
      // 极淡深蓝棋盘格 = 像素画布底纹
      type: 'checker',
      color: 'rgba(29, 43, 83, 0.05)',
      size: '16px',
      opacity: 0.6,
    },
    sidebar: {
      type: 'solid',
      value: '#ffe3cc',
      texture: { type: 'checker', color: 'rgba(29, 43, 83, 0.05)', size: '16px', opacity: 0.5 },
    },
    chat: {
      type: 'solid',
      value: '#fff1e8',
    },
  },
}
