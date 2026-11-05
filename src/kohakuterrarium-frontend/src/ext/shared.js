/**
 * Exposes the dashboard's own module instances to package UI code.
 *
 * ``/ext-shared/vue.js`` and ``/ext-shared/sdk.js`` (generated at build
 * time) await ``globalThis.__KT_SHARED__.load(name)`` and re-export the
 * result, so an extension importing ``vue`` gets this app's Vue, not a
 * second copy. ``element-plus`` is served as a prebuilt file instead.
 */

import * as Vue from "vue"

import * as Sdk from "@/ext/sdk/index"
import { SDK_VERSION } from "@/ext/sdk/version"

export const SHARED_MODULE_LOADERS = {
  vue: async () => Vue,
  "@kohakuterrarium/ui-sdk": async () => Sdk,
}

/** Install ``__KT_SHARED__`` on ``target`` (defaults to ``globalThis``). */
export function installSharedModules(target = globalThis) {
  const cache = new Map()
  target.__KT_SHARED__ = {
    sdkVersion: SDK_VERSION,
    load(name) {
      const loader = SHARED_MODULE_LOADERS[name]
      if (!loader) return Promise.reject(new Error(`no shared module named ${name}`))
      if (!cache.has(name)) cache.set(name, loader())
      return cache.get(name)
    },
  }
  return target.__KT_SHARED__
}
