/** Host registration for the browser theme preference. */
import type { Context } from '@deepseek-ai/cordis';
export { DEFAULT_PREFERENCE, THEME_PREFERENCE_FIELD, THEME_PREFERENCES, THEME_SETTINGS_NAMESPACE, type ThemePreference, type ThemeSettings, } from './theme-settings.ts';
/**
 * Register the durable theme section when a settings provider exists.
 * @param ctx - Host context whose optional settings service owns the section.
 */
export declare function apply(ctx: Context): void;
//# sourceMappingURL=index.d.ts.map
