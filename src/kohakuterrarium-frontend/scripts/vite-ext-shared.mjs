/**
 * Vite plugin for the UI extension shared modules.
 *
 * - Emits ``ext-shared/{vue,sdk}.js``: static re-exports of the running
 *   app's module instances (via ``__KT_SHARED__``).
 * - Emits ``ext-shared/element-plus.js`` + ``ext-shared/vendor/``: the
 *   prebuilt Element Plus ESM of the app's version, which imports the
 *   app's Vue through the import map and loads only when asked for.
 * - Emits ``ext-shared/testing/*``: the SDK test double for packages.
 * - Injects the import map that points bare imports at those files,
 *   and serves the same files from the dev server.
 */

import { readFileSync } from "node:fs"
import { createRequire } from "node:module"
import { fileURLToPath } from "node:url"

const sdkDir = () => fileURLToPath(new URL("../src/ext/sdk/", import.meta.url))
const resolveDep = (id) => createRequire(import.meta.url).resolve(id)
const TESTING_FILES = ["mock.js", "version.js", "frameBridge.js", "theme.js"]
const IDENT_RE = /^[A-Za-z_$][\w$]*$/

export const SHARED_SPECIFIERS = {
  vue: "vue.js",
  "element-plus": "element-plus.js",
  "@kohakuterrarium/ui-sdk": "sdk.js",
}

async function exportNames(specifier) {
  if (specifier === "@kohakuterrarium/ui-sdk") {
    const { SDK_EXPORT_NAMES } = await import(new URL("../src/ext/sdk/exports.js", import.meta.url))
    return SDK_EXPORT_NAMES
  }
  return Object.keys(await import(specifier))
}

/** Source of ``/ext-shared/<file>``: await the app's instance, re-export every name. */
export function proxySource(specifier, names) {
  const valid = [...new Set(names)].filter((n) => n !== "default" && IDENT_RE.test(n)).sort()
  return [
    `const __m = await globalThis.__KT_SHARED__.load(${JSON.stringify(specifier)})`,
    "export default __m.default ?? __m",
    `export const { ${valid.join(", ")} } = __m`,
    "",
  ].join("\n")
}

/** ``/ext-shared/element-plus.js``: add the stylesheet once, re-export the vendor build. */
export const ELEMENT_PLUS_SOURCE = [
  'const href = new URL("./vendor/element-plus.css", import.meta.url).href',
  'if (typeof document !== "undefined" && !document.querySelector("link[data-kt-shared=element-plus]")) {',
  '  const link = document.createElement("link")',
  '  link.rel = "stylesheet"',
  "  link.href = href",
  '  link.dataset.ktShared = "element-plus"',
  "  document.head.appendChild(link)",
  "}",
  'export * from "./vendor/element-plus.mjs"',
  'export { default } from "./vendor/element-plus.mjs"',
  "",
].join("\n")

// A selector list made only of :root / html / .dark variants (the theme scope).
const THEME_SELECTOR_RE = /^(:root|html)?(\.dark|\[data-theme[^\]]*\])?$|^\.dark$/

function isThemeSelector(selectors) {
  return selectors
    .split(",")
    .map((s) => s.trim())
    .every((s) => s && THEME_SELECTOR_RE.test(s))
}

function withoutCustomProperties(body) {
  return body
    .split(";")
    .filter((decl) => decl.trim() && !decl.trim().startsWith("--"))
    .join(";")
}

/**
 * Remove CSS custom-property declarations from top-level rules scoped to
 * ``:root`` / ``html.dark``-style selectors, so the extension copy of
 * Element Plus never overrides the app's theme variables. Every other
 * rule, including at-rule blocks, is kept as written.
 */
export function stripThemeVariables(css) {
  let out = ""
  let i = 0
  while (i < css.length) {
    const open = css.indexOf("{", i)
    if (open === -1) {
      out += css.slice(i)
      break
    }
    const selector = css.slice(i, open)
    let depth = 1
    let j = open + 1
    while (j < css.length && depth > 0) {
      if (css[j] === "{") depth++
      else if (css[j] === "}") depth--
      j++
    }
    const body = css.slice(open + 1, j - 1)
    const trimmed = selector.trim()
    if (!trimmed.startsWith("@") && !body.includes("{") && isThemeSelector(trimmed)) {
      const kept = withoutCustomProperties(body)
      if (kept) out += `${selector}{${kept}}`
    } else {
      out += css.slice(i, j)
    }
    i = j
  }
  return out
}

export function importMap(base = "/") {
  const imports = {}
  for (const [specifier, file] of Object.entries(SHARED_SPECIFIERS))
    imports[specifier] = `${base}ext-shared/${file}`
  return JSON.stringify({ imports })
}

async function sharedFiles() {
  const files = {
    "ext-shared/vue.js": proxySource("vue", await exportNames("vue")),
    "ext-shared/sdk.js": proxySource(
      "@kohakuterrarium/ui-sdk",
      await exportNames("@kohakuterrarium/ui-sdk"),
    ),
    "ext-shared/element-plus.js": ELEMENT_PLUS_SOURCE,
    "ext-shared/vendor/element-plus.mjs": readFileSync(
      resolveDep("element-plus/dist/index.full.min.mjs"),
      "utf-8",
    ),
    "ext-shared/vendor/element-plus.css": stripThemeVariables(
      readFileSync(resolveDep("element-plus/dist/index.css"), "utf-8"),
    ),
  }
  for (const name of TESTING_FILES) {
    const target = name === "mock.js" ? "sdk-mock.js" : name
    files[`ext-shared/testing/${target}`] = readFileSync(`${sdkDir()}${name}`, "utf-8")
  }
  return files
}

function contentType(path) {
  return path.endsWith(".css") ? "text/css" : "text/javascript"
}

export default function extSharedPlugin() {
  let cache = null
  const files = async () => (cache ??= await sharedFiles())
  return {
    name: "kt-ext-shared",
    transformIndexHtml() {
      return [
        {
          tag: "script",
          attrs: { type: "importmap" },
          children: importMap(),
          injectTo: "head-prepend",
        },
      ]
    },
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const path = (req.url || "").split("?")[0].replace(/^\//, "")
        if (!path.startsWith("ext-shared/")) return next()
        const body = (await files())[path]
        if (body === undefined) return next()
        res.setHeader("Content-Type", contentType(path))
        res.end(body)
      })
    },
    async generateBundle() {
      for (const [fileName, source] of Object.entries(await files())) {
        this.emitFile({ type: "asset", fileName, source })
      }
    },
  }
}
