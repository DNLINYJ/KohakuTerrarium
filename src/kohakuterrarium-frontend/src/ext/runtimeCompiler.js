/**
 * Lazily enables Vue's runtime template compiler for UI extensions.
 *
 * The dashboard ships Vue's runtime-only build. Importing the full
 * ``vue.esm-bundler`` build registers its compiler on the same
 * ``@vue/runtime-dom`` instance, so ``template:`` strings in no-build
 * extension components start working. It loads once, on first use.
 */

let _loading = null

export function enableRuntimeCompiler() {
  _loading ??= import("vue/dist/vue.esm-bundler.js").then(() => true)
  return _loading
}
