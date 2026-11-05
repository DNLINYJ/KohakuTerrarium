import { createPinia, setActivePinia } from "pinia"
import { beforeEach, describe, expect, it, vi } from "vitest"

import { useSessionDetailStore } from "./sessionDetail.js"
import { sessionAPI } from "@/utils/api"
import { acquireScope, releaseScope } from "@/composables/useScope"

vi.mock("@/utils/api", () => ({
  sessionAPI: {
    getHistoryIndex: vi.fn(),
    getTree: vi.fn().mockResolvedValue(null),
    getSummary: vi.fn().mockResolvedValue(null),
  },
}))

beforeEach(() => {
  setActivePinia(createPinia())
  sessionAPI.getHistoryIndex.mockReset().mockResolvedValue({ meta: {}, targets: [] })
  sessionAPI.getTree.mockReset().mockResolvedValue(null)
  sessionAPI.getSummary.mockReset().mockResolvedValue(null)
})

function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}

describe("sessionDetail current discovery identity", () => {
  it.each(["success", "error"])(
    "ignores old %s/catch/finally across A-B-A and independent scopes",
    async (outcome) => {
      const old = [deferred(), deferred(), deferred()]
      sessionAPI.getHistoryIndex.mockReturnValueOnce(old[0].promise)
      sessionAPI.getTree.mockReturnValueOnce(old[1].promise)
      sessionAPI.getSummary.mockReturnValueOnce(old[2].promise)
      const detail = useSessionDetailStore("binding")
      const first = detail.load("A")
      await detail.load("B")
      const current = [deferred(), deferred(), deferred()]
      sessionAPI.getHistoryIndex.mockReturnValueOnce(current[0].promise)
      sessionAPI.getTree.mockReturnValueOnce(current[1].promise)
      sessionAPI.getSummary.mockReturnValueOnce(current[2].promise)
      const third = detail.load("A")
      const other = useSessionDetailStore("other")
      sessionAPI.getSummary.mockResolvedValueOnce({ agents: ["separate"] })
      await other.load("A")
      if (outcome === "error") old.forEach((request) => request.reject(new Error("stale")))
      else
        old.forEach((request) =>
          request.resolve({ meta: { agents: ["old"] }, targets: ["old"], agents: ["old"] }),
        )
      await first
      expect(detail.bindingGeneration).toBe(3)
      expect(detail.meta).toBeNull()
      expect(detail.tree).toBeNull()
      expect(detail.summary).toBeNull()
      expect(detail.error).toBe("")
      expect(detail.summaryError).toBe("")
      expect([detail.loadingMeta, detail.loadingTree, detail.loadingSummary]).toEqual([
        true,
        true,
        true,
      ])
      expect(detail.traceDiscovery.status).toBe("pending")
      current[0].resolve({ meta: { agents: ["alice"] }, targets: ["alice"] })
      current[1].resolve({ name: "new tree" })
      current[2].resolve({ agents: ["bob"] })
      await third
      expect(detail.traceDiscovery).toEqual({ status: "ready", agent: "bob" })
      expect(detail.tree).toEqual({ name: "new tree" })
      expect([detail.loadingMeta, detail.loadingTree, detail.loadingSummary]).toEqual([
        false,
        false,
        false,
      ])
      expect(other.traceDiscovery).toEqual({ status: "ready", agent: "separate" })
    },
  )

  it("separates filtered summary display from current unfiltered discovery, including a refresh", async () => {
    const detail = useSessionDetailStore()
    const summary = deferred()
    sessionAPI.getSummary.mockReturnValueOnce(summary.promise)
    const load = detail.load("A")
    sessionAPI.getSummary.mockResolvedValueOnce({ agents: ["filtered"] })
    await detail.loadSummary("filtered")
    expect(detail.summary.agents).toEqual(["filtered"])
    expect(detail.traceDiscovery.status).toBe("pending")
    summary.resolve({ agents: ["ch:team", "bob", "alice"] })
    await load
    expect(detail.summary.agents).toEqual(["filtered"])
    expect(detail.traceDiscovery).toEqual({ status: "ready", agent: "bob" })
    const stale = deferred(),
      fresh = deferred()
    sessionAPI.getSummary.mockReturnValueOnce(stale.promise)
    const previous = detail.loadSummary()
    sessionAPI.getSummary.mockReturnValueOnce(fresh.promise)
    const reload = detail.load("A")
    stale.resolve({ agents: ["old"] })
    await previous
    expect(detail.traceDiscovery.status).toBe("pending")
    expect(detail.loadingSummary).toBe(true)
    fresh.resolve({ agents: ["fresh"] })
    await reload
    expect(detail.traceDiscovery).toEqual({ status: "ready", agent: "fresh" })
    expect(detail.bindingGeneration).toBe(1)
    expect(detail.loadEpoch).toBe(2)
  })

  it("uses only settled valid index candidates and honors an eligible preferred fallback", async () => {
    const detail = useSessionDetailStore()
    sessionAPI.getSummary.mockRejectedValueOnce(new Error("summary failed"))
    sessionAPI.getHistoryIndex.mockResolvedValueOnce({
      targets: ["ch:team", "alice", "alice", null],
      meta: { agents: ["bob", "alice"], viewer_default_agent: "bob" },
    })
    await detail.load("A")
    expect(detail.traceDiscovery).toEqual({ status: "ready", agent: "bob" })
    expect(detail.summaryStatus).toBe("error")
    expect(detail.summaryError).toContain("summary failed")
    sessionAPI.getHistoryIndex.mockResolvedValueOnce({
      targets: ["alice"],
      meta: { viewer_default_agent: "unknown-attached" },
    })
    await detail.load("A")
    expect(detail.traceDiscovery).toEqual({ status: "ready", agent: "alice" })
  })
})

describe("saved Conversation summary scheduling", () => {
  it("restores aggregate totals after joining discovery superseded by a filtered display", async () => {
    const detail = useSessionDetailStore("aggregate")
    const aggregate = deferred()
    sessionAPI.getSummary.mockReturnValueOnce(aggregate.promise)
    const load = detail.load("A")
    sessionAPI.getSummary.mockResolvedValueOnce({ agents: ["alice"], totals: { tokens: 1 } })
    await detail.loadSummary("alice")
    const activation = detail.setTab("cost")
    aggregate.resolve({ agents: ["alice", "bob"], totals: { tokens: 20 } })
    await Promise.all([load, activation])
    expect(detail.summary.totals.tokens).toBe(20)
    expect(detail.loadingSummary).toBe(false)
    const lateFiltered = deferred()
    sessionAPI.getSummary.mockReturnValueOnce(lateFiltered.promise)
    const filtered = detail.loadSummary("alice")
    await detail.ensureSummary()
    lateFiltered.resolve({ agents: ["alice"], totals: { tokens: 2 } })
    await filtered
    expect(detail.summary.totals.tokens).toBe(20)
    expect(detail.loadingSummary).toBe(false)
  })

  it("invalidates pending discovery across reset and scope release", async () => {
    acquireScope("reset")
    const detail = useSessionDetailStore("reset")
    const old = deferred()
    sessionAPI.getSummary.mockReturnValueOnce(old.promise)
    const first = detail.load("A")
    detail.$reset()
    const fresh = deferred()
    sessionAPI.getSummary.mockReturnValueOnce(fresh.promise)
    const second = detail.load("A")
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(2)
    old.resolve({ agents: ["old"] })
    await first
    expect(detail.summaryStatus).toBe("pending")
    expect(detail.summary).toBeNull()
    releaseScope("reset")
    fresh.resolve({ agents: ["disposed"] })
    await second
    expect(detail.summary).toBeNull()
  })

  it("discovers ordered viewer agents without changing history targets or inventing summary success", async () => {
    const detail = useSessionDetailStore("saved")
    sessionAPI.getHistoryIndex.mockResolvedValue({
      meta: { agents: ["alice", "bob"] },
      targets: ["alice", "bob", "ch:team"],
      viewer_agents: ["alice:attached:review:0", "bob", "alice"],
    })
    await detail.load("A", { tab: "conv" })
    expect(sessionAPI.getSummary).not.toHaveBeenCalled()
    expect(detail.summaryStatus).toBe("idle")
    expect(detail.summary).toBeNull()
    expect(detail.traceDiscovery.status).toBe("pending")
    expect(detail.agents).toEqual(["alice:attached:review:0", "bob", "alice"])
    expect(detail.targets).toEqual(["alice", "bob", "ch:team"])
    const epoch = detail.loadEpoch
    const pending = deferred()
    sessionAPI.getSummary.mockReturnValueOnce(pending.promise)
    detail.setTab("trace")
    detail.setTab("cost")
    detail.setTab("overview")
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(1)
    expect(detail.loadEpoch).toBe(epoch)
    expect(detail.traceDiscovery.status).toBe("pending")
    pending.resolve({ agents: ["bob", "alice"], totals: { tokens: 123 } })
    await detail.ensureSummary()
    detail.setTab("conv")
    detail.setTab("cost")
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(1)
    expect(detail.summary.totals.tokens).toBe(123)
    expect(detail.traceDiscovery.agent).toBe("bob")
    detail.setTab("conv")
    await detail.load("A")
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(1)
    expect(detail.summaryStatus).toBe("idle")
    detail.setTab("trace")
    await detail.ensureSummary()
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(2)
  })

  it("retries failed summaries and preserves live Conversation refresh totals", async () => {
    const detail = useSessionDetailStore("retry")
    await detail.load("A", { tab: "conv" })
    sessionAPI.getSummary.mockRejectedValueOnce(new Error("try again"))
    detail.setTab("trace")
    await detail.ensureSummary()
    expect(detail.summaryStatus).toBe("error")
    detail.setTab("conv")
    sessionAPI.getSummary.mockResolvedValue({ agents: ["alice"], totals: { tokens: 42 } })
    detail.setTab("cost")
    await detail.ensureSummary()
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(2)
    expect(detail.summary.totals.tokens).toBe(42)
    detail.setTab("conv")
    detail.live = true
    await detail.load("A")
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(3)
    sessionAPI.getSummary.mockResolvedValue({ agents: ["alice"], totals: { tokens: 84 } })
    await detail.load("A")
    expect(detail.summary.totals.tokens).toBe(84)
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(4)
  })

  it("ignores a pending summary from a previous binding and shares nothing across scopes", async () => {
    const detail = useSessionDetailStore("one")
    const old = deferred()
    sessionAPI.getSummary.mockReturnValueOnce(old.promise)
    const load = detail.load("A")
    await detail.load("B", { tab: "conv" })
    await detail.load("A", { tab: "conv" })
    old.resolve({ agents: ["obsolete"], totals: { tokens: 99 } })
    await load
    expect(detail.summaryStatus).toBe("idle")
    expect(detail.loadingSummary).toBe(false)
    expect(detail.summary).toBeNull()
    const other = useSessionDetailStore("two")
    sessionAPI.getSummary.mockResolvedValue({ agents: ["separate"] })
    await other.load("A")
    expect(other.summary.agents).toEqual(["separate"])
    expect(detail.summary).toBeNull()
  })
})

describe("sessionDetail store — agents getter (UXI-03)", () => {
  it("enumerates every creature, not just root, unioning summary + targets", () => {
    const detail = useSessionDetailStore("s1")
    detail.meta = { agents: ["root"] }
    detail.targets = ["root", "worker_a", "ch:team"]
    detail.summary = { agents: ["root", "worker_b"] }
    // Summary first (viewer-default ordering), then targets, deduped;
    // the ``ch:`` channel target is excluded.
    expect(detail.agents).toEqual(["root", "worker_b", "worker_a"])
    expect(detail.primaryAgent).toBe("root")
  })

  it("falls back to meta.agents before summary/targets load", () => {
    const detail = useSessionDetailStore("s2")
    detail.meta = { agents: ["alice", "bob"] }
    detail.targets = []
    detail.summary = null
    expect(detail.agents).toEqual(["alice", "bob"])
  })

  it("returns an empty list when nothing is loaded", () => {
    const detail = useSessionDetailStore("s3")
    expect(detail.agents).toEqual([])
    expect(detail.primaryAgent).toBeNull()
  })
})

describe("sessionDetail store — loadMeta (UXI-01)", () => {
  it("treats a 404 from the live-id index as a benign empty state", async () => {
    const { sessionAPI } = await import("@/utils/api")
    sessionAPI.getHistoryIndex.mockRejectedValueOnce({ response: { status: 404 } })
    const detail = useSessionDetailStore("live1")
    detail.name = "live_graph_new"
    await detail.loadMeta()
    expect(detail.error).toBe("")
    expect(detail.meta).toBeNull()
    expect(detail.targets).toEqual([])
  })

  it("still surfaces a non-404 failure as an error", async () => {
    const { sessionAPI } = await import("@/utils/api")
    sessionAPI.getHistoryIndex.mockRejectedValueOnce({ response: { status: 500 }, message: "boom" })
    const detail = useSessionDetailStore("live2")
    detail.name = "broken_session"
    await detail.loadMeta()
    expect(detail.error).toContain("Failed to load session metadata")
  })
})
