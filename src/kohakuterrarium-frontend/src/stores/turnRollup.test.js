import { createPinia, setActivePinia } from "pinia"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useTurnRollupStore } from "@/stores/turnRollup"
import { sessionAPI } from "@/utils/api"

vi.mock("@/utils/api", () => ({
  sessionAPI: { getTurns: vi.fn() },
}))

function deferred() {
  let resolve
  let reject
  const promise = new Promise((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}

describe("turn rollup store", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    sessionAPI.getTurns.mockReset()
  })

  it("ignores a stale response after switching agents", async () => {
    const oldRequest = deferred()
    sessionAPI.getTurns
      .mockReturnValueOnce(oldRequest.promise)
      .mockResolvedValueOnce({ agent: "bob", turns: [{ turn_index: 2 }], total: 1 })
    const store = useTurnRollupStore("turn-rollup-race")

    const firstLoad = store.load("session", "alice")
    await Promise.resolve()
    await store.load("session", "bob")
    oldRequest.resolve({ agent: "alice", turns: [{ turn_index: 1 }], total: 1 })
    await firstLoad

    expect(store.agent).toBe("bob")
    expect(store.turns).toEqual([{ turn_index: 2 }])
    expect(store.total).toBe(1)
    expect(store.error).toBe("")
    expect(store.loading).toBe(false)
  })

  it("keeps a new same-agent older-page spinner when the previous generation settles", async () => {
    const store = useTurnRollupStore("older-generation")
    sessionAPI.getTurns.mockResolvedValue({
      agent: "alice",
      turns: [{ turn_index: 1001 }],
      total: 2000,
      offset: 1000,
    })
    await store.load("session", "alice")
    const old = deferred(),
      current = deferred()
    sessionAPI.getTurns.mockReturnValueOnce(old.promise)
    const oldLoad = store.loadOlder()
    await store.load("session", "alice")
    expect(store.loadingOlder).toBe(false)
    sessionAPI.getTurns.mockReturnValueOnce(current.promise)
    const newLoad = store.loadOlder()
    expect(store.loadingOlder).toBe(true)
    old.resolve({ turns: [{ turn_index: 1 }], offset: 0 })
    await oldLoad
    expect(store.loadingOlder).toBe(true)
    expect(store.turns).toEqual([{ turn_index: 1001 }])
    current.resolve({ turns: [{ turn_index: 2 }], offset: 0 })
    await newLoad
    expect(store.loadingOlder).toBe(false)
    expect(store.turns.map((t) => t.turn_index)).toEqual([2, 1001])
  })

  it("invalidates an in-flight response when cleared", async () => {
    const request = deferred()
    sessionAPI.getTurns.mockReturnValueOnce(request.promise)
    const store = useTurnRollupStore("turn-rollup-clear")

    const load = store.load("session", "alice")
    await Promise.resolve()
    store.clear()
    request.resolve({ agent: "alice", turns: [{ turn_index: 1 }], total: 1 })
    await load

    expect(store.sessionName).toBe("")
    expect(store.agent).toBe("")
    expect(store.turns).toEqual([])
    expect(store.loading).toBe(false)
  })
})
