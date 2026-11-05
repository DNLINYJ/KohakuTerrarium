import { createPinia, setActivePinia } from "pinia"
import { nextTick, ref } from "vue"
import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/utils/api", () => ({ default: { post: vi.fn() } }))

import { OwnerCallError, createOwnerClient } from "@/ext/ownerClient"
import {
  closeOwnerSocket,
  ownerSubscriptionCount,
  setOwnerSocketFactory,
  subscribeOwner,
} from "@/ext/ownerSocket"
import api from "@/utils/api"

class FakeSocket {
  static all = []
  constructor(url) {
    this.url = url
    this.sent = []
    this.readyState = 0
    FakeSocket.all.push(this)
  }
  open() {
    this.readyState = 1
    this.onopen?.()
  }
  send(text) {
    this.sent.push(JSON.parse(text))
  }
  push(message) {
    this.onmessage?.({ data: JSON.stringify(message) })
  }
  close() {
    this.readyState = 3
    this.onclose?.()
  }
}

function notLoaded() {
  const err = new Error("404")
  err.response = {
    status: 404,
    data: { detail: { error: "owner_not_loaded", message: "plugin:x is not loaded" } },
  }
  return err
}

const flush = () => new Promise((r) => setTimeout(r, 0))

describe("owner client", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    FakeSocket.all = []
    closeOwnerSocket({ forget: true })
    setOwnerSocketFactory((url) => new FakeSocket(url))
  })

  it("reads state, calls actions and follows pushes for its scope", async () => {
    api.post.mockImplementation(async (url, body) =>
      url.endsWith("/state")
        ? { data: { state: { n: 1 } } }
        : { data: { result: body.args.n * 2 } },
    )
    const scope = ref({ sessionId: "s1", creatureId: "alice" })
    const client = createOwnerClient("plugin:counter", scope)
    client.start()
    await flush()
    expect(client.status.value).toBe("ready")
    expect(client.state.value).toEqual({ n: 1 })
    expect(api.post).toHaveBeenCalledWith(
      "/extensions/owner/state",
      { owner: "plugin:counter", session_id: "s1", creature_id: "alice" },
      { __kt_auth_retry: true },
    )
    expect(await client.call("double", { n: 4 })).toBe(8)

    const socket = FakeSocket.all[0]
    expect(socket.url).toContain("/ws/extensions/owner")
    socket.open()
    const sub = socket.sent[0]
    expect(sub).toMatchObject({
      op: "subscribe",
      owner: "plugin:counter",
      session_id: "s1",
      creature_id: "alice",
    })
    const seen = []
    client.on("tick", (d) => seen.push(d))
    client.on("*", (e) => seen.push(`*${e}`))
    socket.push({ type: "event", id: sub.id, event: "state", data: { n: 9 } })
    socket.push({ type: "event", id: sub.id, event: "tick", data: 3 })
    socket.push({ type: "event", id: "someone-else", event: "tick", data: 99 })
    expect(client.state.value).toEqual({ n: 9 })
    expect(seen).toEqual(["*state", 3, "*tick"])

    scope.value = { sessionId: "s1", creatureId: "bob" }
    await nextTick()
    await flush()
    // Switching scope reuses the open socket: subscribe new, then drop old.
    expect(FakeSocket.all).toHaveLength(1)
    expect(socket.readyState).toBe(1)
    expect(socket.sent.at(-2)).toMatchObject({ op: "subscribe", creature_id: "bob" })
    expect(socket.sent.at(-1)).toEqual({ op: "unsubscribe", id: sub.id })

    client.dispose()
    expect(ownerSubscriptionCount()).toBe(0)
    expect(socket.readyState).toBe(3)
  })

  it("marks an unloaded or out-of-scope owner absent and rejects its calls", async () => {
    api.post.mockRejectedValue(notLoaded())
    const client = createOwnerClient(
      "plugin:counter",
      ref({ sessionId: "s1", creatureId: "alice" }),
    )
    await client.refresh()
    expect(client.status.value).toBe("absent")
    expect(client.state.value).toBeNull()
    const err = await client.call("x").catch((e) => e)
    expect(err).toBeInstanceOf(OwnerCallError)
    expect(err.code).toBe("owner_not_loaded")
    expect(err.status).toBe(404)

    vi.clearAllMocks()
    const noScope = createOwnerClient("tool:send", ref({ sessionId: "", creatureId: "" }))
    await noScope.refresh()
    expect(noScope.status.value).toBe("absent")
    await expect(noScope.call("x")).rejects.toMatchObject({ code: "owner_not_loaded" })
    expect(api.post).not.toHaveBeenCalled()
  })

  it("sends no scope for package owners and reports server errors", async () => {
    const err = new Error("500")
    err.response = { status: 500, data: { detail: { error: "owner_failed", message: "boom" } } }
    api.post.mockRejectedValue(err)
    const client = createOwnerClient("package:alpha", ref({ sessionId: "s", creatureId: "c" }))
    await client.refresh()
    expect(client.status.value).toBe("error")
    expect(client.error.value).toBe("boom")
    expect(api.post.mock.calls[0][1]).toEqual({
      owner: "package:alpha",
      session_id: "",
      creature_id: "",
    })
  })

  it("re-reads state after the server dropped pushes, and treats disabled owners as absent", async () => {
    api.post.mockResolvedValue({ data: { state: { n: 1 } } })
    const client = createOwnerClient("package:alpha", ref({}))
    client.start()
    await flush()
    const socket = FakeSocket.all[0]
    socket.open()
    api.post.mockResolvedValue({ data: { state: { n: 5 } } })
    socket.push({ type: "overflow", dropped: 12 })
    await flush()
    expect(client.state.value).toEqual({ n: 5 })

    const err = new Error("404")
    err.response = { status: 404, data: { detail: { error: "owner_disabled", message: "off" } } }
    api.post.mockRejectedValue(err)
    await client.refresh()
    expect(client.status.value).toBe("absent")
    client.dispose()
  })

  it("re-subscribes everything after the socket reconnects", async () => {
    vi.useFakeTimers()
    try {
      const off = subscribeOwner({ owner: "package:a" }, () => {})
      subscribeOwner({ owner: "package:b" }, () => {})
      FakeSocket.all[0].open()
      expect(FakeSocket.all[0].sent.map((m) => m.owner)).toEqual(["package:a", "package:b"])
      FakeSocket.all[0].close()
      vi.advanceTimersByTime(600)
      FakeSocket.all[1].open()
      expect(FakeSocket.all[1].sent.map((m) => m.owner)).toEqual(["package:a", "package:b"])
      off()
      expect(ownerSubscriptionCount()).toBe(1)
    } finally {
      vi.useRealTimers()
    }
  })
})
