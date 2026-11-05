/**
 * UI contributions of built-in core modules.
 *
 * They use the same contribution records and ``register(kt)`` entry as
 * installed packages; the entry is a lazy chunk of this app (web_dist)
 * instead of a file under ``/api/ext/<pkg>/ui/``.
 */

export const BUILTIN_UI_PACKAGE = "@builtin"

export const BUILTIN_UI_PACKAGES = [
  {
    package: BUILTIN_UI_PACKAGE,
    version: "",
    sdk: ">=1,<2",
    entry: "",
    styles: [],
    i18n: {},
    has_api: false,
    has_handler: false,
    dev_url: "",
    contributes: [
      {
        kind: "panel",
        id: "budget",
        label: "Budget",
        owner: "plugin:budget",
        icon: "i-carbon-meter",
        description: "Turn, tool-call and walltime usage of the budget plugin.",
        order: 100,
        minDensity: "compact",
        isolation: "inline",
      },
    ],
    load: () => import("./entry.js"),
  },
]
