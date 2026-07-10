/**
 * 媒体展示组件模块
 *
 * 提供音频播放器和图像画廊组件，用于展示 TTS 输出和生成图像。
 *
 * 组件：
 * - AudioPlayer：音频播放器（支持播放/暂停/下载，可嵌入消息流）
 * - ImageGallery：图像画廊（网格展示、Lightbox 大图、生成参数显示）
 */

export { AudioPlayer } from './AudioPlayer'
export type { AudioPlayerProps } from './AudioPlayer'

export { ImageGallery } from './ImageGallery'
export type { ImageGalleryProps, ImageItem } from './ImageGallery'
