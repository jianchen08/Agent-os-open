import { settingsNamespace } from "@deepseek-ai/dsh-settings";
import z from "@deepseek-ai/schemastery";
//#region lib/types/theme-settings.js
/** Theme preferences stored in the Host user-settings document. */
/** Built-in preferences accepted at the registry and settings boundaries. */
const THEME_PREFERENCES = [
	"light",
	"dark",
	"system"
];
/** Settings namespace owned by the theme plugin. */
const THEME_SETTINGS_NAMESPACE = "ui-theme";
/** Field carrying the selected built-in theme preference. */
const THEME_PREFERENCE_FIELD = "preference";
/** Default preference when the user-settings document has no override. */
const DEFAULT_PREFERENCE = "system";
/** Durable theme schema; also the wire envelope the browser scope validates against. */
const ThemeSettingsSchema = z.object({ [THEME_PREFERENCE_FIELD]: z.union([...THEME_PREFERENCES]).default(DEFAULT_PREFERENCE) });
//#endregion
//#region lib/types/index.js
/** Host registration for the browser theme preference. */
/**
* Register the durable theme section when a settings provider exists.
* @param ctx - Host context whose optional settings service owns the section.
*/
function apply(ctx) {
	ctx.inject(["settings"], (settingsCtx) => {
		settingsCtx.settings.register(settingsNamespace(THEME_SETTINGS_NAMESPACE), ThemeSettingsSchema);
	});
}
//#endregion
export { DEFAULT_PREFERENCE, THEME_PREFERENCES, THEME_PREFERENCE_FIELD, THEME_SETTINGS_NAMESPACE, apply };
