/**
 * Client side of the owner RPC: ``state`` / ``call`` over HTTP, pushes
 * over the shared owner socket. One client serves one contribution and
 * follows its scope (session + focused creature) as it changes.
 */

import { computed, ref, shallowRef, watch } from "vue"

import { subscribeOwner } from "@/ext/ownerSocket"
import api from "@/utils/api"

// ``__kt_auth_retry`` is the api.js retry flag: a 401 rejects instead of opening a login dialog.
const QUIET = { __kt_auth_retry: true }
const ABSENT_CODES = new Set([
  "owner_not_loaded",
  "owner_disabled",
  "owner_has_no_ui",
  "owner_unsupported",
])

/** Error thrown by ``call``: ``code`` is the server's ``detail.error``. */
export class OwnerCallError extends Error {
  constructor(code, message, status) {
    super(message || code)
    this.name = "OwnerCallError"
    this.code = code
    this.status = status
  }
}

function toError(err) {
  const detail = err?.response?.data?.detail
  if (detail && typeof detail === "object")
    return new OwnerCallError(detail.error, detail.message, err.response.status)
  return new OwnerCallError("owner_failed", err?.message || String(err), err?.response?.status)
}

export function ownerKind(owner) {
  return String(owner || "").split(":", 1)[0]
}

/**
 * ``scope`` is a ref/getter of ``{ sessionId, creatureId }``; package owners ignore it.
 * ``status``: idle | loading | ready | absent | error.
 */
export function createOwnerClient(owner, scope) {
  const state = shallowRef(null)
  const status = ref("idle")
  const error = ref("")
  const handlers = new Map()
  const isRuntime = ownerKind(owner) !== "package"
  const params = computed(() => {
    const s = typeof scope === "function" ? scope() : scope?.value
    return {
      owner,
      sessionId: isRuntime ? s?.sessionId || "" : "",
      creatureId: isRuntime ? s?.creatureId || "" : "",
    }
  })
  let unsubscribe = null
  let stopWatch = null
  let generation = 0

  function body(extra = {}) {
    const p = params.value
    return { owner: p.owner, session_id: p.sessionId, creature_id: p.creatureId, ...extra }
  }

  function scopeMissing() {
    return isRuntime && (!params.value.sessionId || !params.value.creatureId)
  }

  function dispatch(event, data) {
    if (event === "state") state.value = data
    for (const handler of handlers.get(event) || []) handler(data)
    for (const handler of handlers.get("*") || []) handler(event, data)
  }

  async function refresh() {
    const mine = ++generation
    if (scopeMissing()) {
      status.value = "absent"
      state.value = null
      return null
    }
    status.value = status.value === "ready" ? "ready" : "loading"
    try {
      const { data } = await api.post("/extensions/owner/state", body(), QUIET)
      if (mine !== generation) return state.value
      state.value = data.state
      status.value = "ready"
      error.value = ""
      return data.state
    } catch (err) {
      if (mine !== generation) return state.value
      const e = toError(err)
      state.value = null
      status.value = ABSENT_CODES.has(e.code) ? "absent" : "error"
      error.value = e.message
      return null
    }
  }

  async function call(action, args = {}) {
    if (scopeMissing())
      throw new OwnerCallError("owner_not_loaded", `${owner} has no creature in scope`)
    try {
      const { data } = await api.post("/extensions/owner/call", body({ action, args }), QUIET)
      return data.result
    } catch (err) {
      throw toError(err)
    }
  }

  function on(event, handler) {
    if (!handlers.has(event)) handlers.set(event, new Set())
    handlers.get(event).add(handler)
    return () => handlers.get(event)?.delete(handler)
  }

  function resubscribe() {
    // Subscribe before dropping the old one so the shared socket stays open.
    const previous = unsubscribe
    unsubscribe = scopeMissing()
      ? null
      : subscribeOwner(params.value, (message) => {
          if (message.type === "event") dispatch(message.event, message.data)
          else if (message.type === "overflow") refresh()
          else if (message.type === "error" && ABSENT_CODES.has(message.error))
            status.value = "absent"
        })
    previous?.()
  }

  function start() {
    if (stopWatch) return
    stopWatch = watch(
      () => `${params.value.sessionId}\u0000${params.value.creatureId}`,
      () => {
        resubscribe()
        refresh()
      },
      { immediate: true },
    )
  }

  function dispose() {
    stopWatch?.()
    stopWatch = null
    unsubscribe?.()
    unsubscribe = null
    handlers.clear()
    generation++
  }

  return { owner, state, status, error, refresh, call, on, start, dispose }
}
