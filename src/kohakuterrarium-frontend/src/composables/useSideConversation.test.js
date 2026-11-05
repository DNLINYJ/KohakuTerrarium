import { effectScope, reactive, ref } from "vue"
import { expect, it } from "vitest"

import { useSideConversation } from "./useSideConversation"

it("scopes delayed responses and close/reopen by session, creature and job", () => {
  const scope = effectScope()
  const chat = reactive({
    _instanceId: "session",
    _instanceGraphId: "graph",
    _instanceGeneration: 1,
  })
  const tabKey = ref("a")
  const side = scope.run(() => useSideConversation({ chat, tabKey }))
  const payload = { type: "side_conversation", job_id: "job-a", name: "fork" }
  tabKey.value = "b"
  side.open(payload, { sessionId: "graph", creatureId: "a" })
  expect(side.active.value).toBeNull()
  side.open({ ...payload, job_id: "job-b" }, { sessionId: "graph", creatureId: "b" })
  expect(side.active.value.job_id).toBe("job-b")
  tabKey.value = "a"
  expect(side.active.value.job_id).toBe("job-a")
  side.close()
  expect(side.active.value).toBeNull()
  side.open(payload, { sessionId: "graph", creatureId: "a" })
  expect(side.active.value.parent).toBe("a")
  chat._instanceGraphId = "other"
  expect(side.active.value).toBeNull()
  chat._instanceGeneration++
  chat._instanceGraphId = "graph"
  expect(side.active.value).toBeNull()
  scope.stop()
})

it("applies metadata to its original scope only and rejects replaced thread identities", () => {
  const scope = effectScope()
  const chat = reactive({ _instanceId: "session", _instanceGeneration: 1 })
  const tabKey = ref("a")
  const side = scope.run(() => useSideConversation({ chat, tabKey }))
  const payload = { type: "side_conversation", job_id: "job", title: "Initial", status: "running" }
  side.open(payload, { sessionId: "session", creatureId: "a" })
  const original = side.active.value
  tabKey.value = "b"
  side.open(payload, { sessionId: "session", creatureId: "b" })
  side.updateMetadata({
    thread: original,
    meta: { title: "Refined", status: "done", job_id: "wrong", parent: "b" },
  })
  expect(side.active.value.title).toBe("Initial")
  expect(side.active.value.status).toBe("running")
  tabKey.value = "a"
  expect(side.active.value.title).toBe("Refined")
  expect(side.active.value.status).toBe("done")
  expect(side.active.value.job_id).toBe("job")
  expect(side.active.value.parent).toBe("a")
  side.updateMetadata({ thread: original, meta: { title: " ", status: "invalid" } })
  expect(side.active.value.title).toBe("Refined")
  expect(side.active.value.status).toBe("done")
  side.close()
  expect(side.remembered.value[0].title).toBe("Refined")
  chat._instanceGeneration++
  side.open(payload, { sessionId: "session", creatureId: "a" })
  side.updateMetadata({ thread: original, meta: { title: "Stale", status: "cancelled" } })
  expect(side.active.value.title).toBe("Initial")
  expect(side.active.value.status).toBe("running")
  scope.stop()
})
