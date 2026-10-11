import { createPinia, setActivePinia } from "pinia"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { useChatStore } from "./chat"
import { agentRows, contextRows } from "@/components/session-v2/model/status/statusModel"
import { terrariumAPI } from "@/utils/api"

function tokenEvent(id, prompt, completion) {
  return {
    type: "token_usage",
    event_id: id,
    turn_index: id,
    branch_id: 0,
    prompt_tokens: prompt,
    completion_tokens: completion,
    total_tokens: prompt + completion,
    _history_key: `e:${id}`,
  }
}

function page(events, { older = true } = {}) {
  return {
    events,
    messages: [],
    history_page: {
      version: 1,
      stream: "events",
      history_id: "h1",
      before: "b1",
      after: "a1",
      has_older: older,
      has_newer: false,
      reset_required: false,
    },
    live_job_ids: [],
    is_processing: false,
  }
}

const usage = (prompt, completion, cached = 0) => ({
  prompt,
  completion,
  cached,
  total: prompt + completion,
})

function instance(creatures) {
  return { id: "g", graph_id: "g", type: "terrarium", has_root: false, creatures }
}

let chat
beforeEach(() => {
  setActivePinia(createPinia())
  chat = useChatStore()
  chat._instanceId = "g"
  chat._instanceGraphId = "g"
  chat._instanceType = "terrarium"
  chat._ws = { close() {}, send() {}, readyState: 1 }
  chat.tabs = ["alice"]
  chat.activeTab = "alice"
})
afterEach(() => {
  chat._ws = null
  chat._cleanup()
  vi.restoreAllMocks()
})

describe("per-creature token usage comes from the backend", () => {
  it("shows a never-opened creature's totals and context from the instance snapshot", () => {
    chat.initForInstance(
      instance([
        { name: "alice", token_usage: usage(100, 10), context_tokens: 100 },
        { name: "bob", token_usage: usage(48000, 2000, 900), context_tokens: 30000 },
      ]),
    )
    const inst = instance([
      { name: "alice", max_context: 0 },
      { name: "bob", max_context: 100000 },
    ])
    const rows = agentRows(inst, { tokenUsage: chat.tokenUsage })
    expect(rows.find((r) => r.name === "bob").tokens).toBe(50000)
    const ctx = contextRows(inst, { tokenUsage: chat.tokenUsage })
    expect(ctx.find((r) => r.name === "bob").pct).toBe(30)
    expect(chat.sessionTokenTotals.total).toBe(50110)
  })

  it("replaces totals on live frames instead of adding them", () => {
    const frame = (totals, context) => ({
      activity_type: "token_usage",
      prompt_tokens: context,
      completion_tokens: 5,
      totals,
      context_tokens: context,
    })
    chat._handleActivity("bob", frame(usage(1000, 100), 400))
    chat._handleActivity("bob", frame(usage(1400, 105), 400))
    expect(chat.tokenUsage.bob.total).toBe(1505)
    expect(chat.tokenUsage.bob.lastPrompt).toBe(400)
  })

  it("seeds a sibling from its attach session_info frame", () => {
    chat._handleActivity("bob", {
      activity_type: "session_info",
      agent_name: "bob",
      max_context: 200000,
      token_usage: usage(9000, 900),
      context_tokens: 7000,
    })
    expect(chat.tokenUsage.bob).toMatchObject({ total: 9900, lastPrompt: 7000, backend: true })
  })

  it("does not lock in a live delta when the tab is opened later", async () => {
    chat._handleActivity("bob", {
      activity_type: "token_usage",
      prompt_tokens: 10,
      completion_tokens: 2,
      totals: usage(5000, 500),
      context_tokens: 10,
    })
    vi.spyOn(terrariumAPI, "getHistoryPage").mockResolvedValue(
      page([tokenEvent(1, 10, 2), tokenEvent(2, 20, 3)]),
    )
    await chat._loadHistory("bob")
    expect(chat.tokenUsage.bob.total).toBe(5500)
    expect(chat.tokenUsage.bob.partial).toBeUndefined()
  })

  it("does not shrink backend totals to a partial history window", async () => {
    chat._applyCreatureUsage("alice", usage(80000, 8000), 12000)
    vi.spyOn(terrariumAPI, "getHistoryPage").mockResolvedValue(
      page([tokenEvent(1, 300, 30)], { older: true }),
    )
    await chat._loadHistory("alice")
    expect(chat.tokenUsage.alice.total).toBe(88000)
    expect(chat.tokenUsage.alice.lastPrompt).toBe(12000)
  })

  it("zeroes context fill on compaction and clear while keeping totals", () => {
    chat._applyCreatureUsage("bob", usage(90000, 900), 85000)
    chat._handleActivity("bob", { activity_type: "compact_complete", round: 1, context_tokens: 0 })
    expect(chat.tokenUsage.bob.lastPrompt).toBe(0)
    expect(chat.tokenUsage.bob.total).toBe(90900)
    chat._applyCreatureUsage("bob", usage(91000, 950), 2000)
    chat._handleActivity("bob", { activity_type: "context_cleared", context_tokens: 0 })
    expect(chat.tokenUsage.bob.lastPrompt).toBe(0)
  })

  it("ignores a snapshot older than the live frames already applied", () => {
    chat._applyCreatureUsage("bob", usage(500, 50), 300)
    chat._adoptCreatureUsage(
      instance([{ name: "bob", token_usage: usage(400, 40), context_tokens: 9 }]),
    )
    expect(chat.tokenUsage.bob.total).toBe(550)
    chat._adoptCreatureUsage(
      instance([{ name: "bob", token_usage: usage(500, 50), context_tokens: 0 }]),
    )
    expect(chat.tokenUsage.bob.lastPrompt).toBe(0)
  })

  it("files the root creature's usage under the root tab", () => {
    chat._rootSourceName = "lead"
    chat._adoptCreatureUsage(
      instance([{ name: "lead", token_usage: usage(10, 1), context_tokens: 10 }]),
    )
    expect(chat.tokenUsage.root.total).toBe(11)
    expect(chat.tokenUsage.lead).toBeUndefined()
  })
})

describe("servers without running totals", () => {
  it("marks a delta-only source partial so its history load rebuilds it", async () => {
    chat._handleActivity("bob", {
      activity_type: "token_usage",
      prompt_tokens: 10,
      completion_tokens: 2,
      total_tokens: 12,
    })
    expect(chat.tokenUsage.bob.partial).toBe(true)
    vi.spyOn(terrariumAPI, "getHistoryPage").mockResolvedValue(
      page([tokenEvent(1, 100, 10), tokenEvent(2, 10, 2)], { older: false }),
    )
    await chat._loadHistory("bob")
    expect(chat.tokenUsage.bob.total).toBe(122)
  })
})
