import { computed, ref, watch } from "vue"

import { terrariumAPI } from "@/utils/api"

/** User-only side-thread windows, scoped to the originating session and creature. */
export function useSideConversation({ chat, tabKey }) {
  const threads = ref({})
  const selected = ref({})
  const scope = (sessionId, creatureId) => JSON.stringify([sessionId, creatureId])
  const currentScope = computed(() =>
    scope(chat._instanceGraphId || chat._instanceId, tabKey.value),
  )
  const active = computed(() => threads.value[selected.value[currentScope.value]] || null)
  const remembered = computed(() =>
    Object.values(threads.value).filter(
      (thread) => scope(thread.sessionId, thread.parent) === currentScope.value,
    ),
  )
  const error = ref("")
  const reopening = ref(false)

  watch(
    () => chat._instanceGeneration,
    () => {
      threads.value = {}
      selected.value = {}
    },
    { flush: "sync" },
  )

  function open(payload, target) {
    if (payload?.type !== "side_conversation" || !payload.job_id || !target?.sessionId) return
    const key = JSON.stringify([target.sessionId, target.creatureId, payload.job_id])
    threads.value[key] = {
      ...payload,
      key,
      sessionId: target.sessionId,
      parent: target.creatureId,
    }
    selected.value[scope(target.sessionId, target.creatureId)] = key
  }

  function updateMetadata({ thread, meta }) {
    if (!thread || threads.value[thread.key] !== thread) return
    if (typeof meta?.title === "string" && meta.title.trim()) thread.title = meta.title
    if (["running", "done", "error", "cancelled"].includes(meta?.status))
      thread.status = meta.status
  }

  function close() {
    delete selected.value[currentScope.value]
  }

  watch(
    currentScope,
    () => {
      error.value = ""
    },
    { flush: "sync" },
  )

  async function reopen(thread) {
    if (reopening.value) return
    const generation = chat._instanceGeneration
    const origin = currentScope.value
    reopening.value = true
    error.value = ""
    try {
      const result = await terrariumAPI.executeCreatureCommand(
        thread.sessionId,
        thread.parent,
        "btw",
        `--open ${thread.job_id}`,
      )
      if (generation !== chat._instanceGeneration) return
      if (result?.error) throw new Error(result.error)
      open(result?.data, { sessionId: thread.sessionId, creatureId: thread.parent })
    } catch (err) {
      if (generation === chat._instanceGeneration && origin === currentScope.value)
        error.value = err?.response?.data?.detail || err?.message || String(err)
    } finally {
      reopening.value = false
    }
  }

  return { active, remembered, reopening, error, open, close, reopen, updateMetadata }
}
