/**
 * In-chat renderer registries: tool name → renderer, trigger name → renderer.
 *
 * Chat blocks look up their tool / trigger here; a hit renders the
 * registered host component in place of the default block body. The
 * first registration of a name wins; later ones are reported and ignored.
 */

import { markRaw, reactive } from "vue"

export const toolRenderers = reactive(new Map())
export const triggerRenderers = reactive(new Map())

function register(map, what, name, entry) {
  const existing = map.get(name)
  if (existing && existing.key !== entry.key) {
    console.warn(
      `[ui-extensions] ${what} renderer for ${name} already provided by ${existing.key}; ignoring ${entry.key}`,
    )
    return false
  }
  map.set(name, { ...entry, component: markRaw(entry.component) })
  return true
}

export function registerToolRenderer(name, entry) {
  return register(toolRenderers, "tool", name, entry)
}

export function registerTriggerRenderer(name, entry) {
  return register(triggerRenderers, "trigger", name, entry)
}

/** Remove every renderer whose contribution key is in ``keys``. */
export function unregisterRenderers(keys) {
  const drop = new Set(keys)
  for (const map of [toolRenderers, triggerRenderers]) {
    for (const [name, entry] of [...map.entries()]) if (drop.has(entry.key)) map.delete(name)
  }
}
