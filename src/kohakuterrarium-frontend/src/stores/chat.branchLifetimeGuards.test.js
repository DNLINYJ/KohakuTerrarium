import { createPinia, setActivePinia } from "pinia"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { _replayEvents, useChatStore } from "./chat.js"

beforeEach(() => {
  setActivePinia(createPinia())
})

afterEach(() => {
  useChatStore()._clearBranchResyncTimers()
  vi.restoreAllMocks()
})

function originalEvents() {
  return [
    { type: "user_input", event_id: 1, content: "A", turn_index: 1, branch_id: 1 },
    { type: "user_message", event_id: 2, content: "A", turn_index: 1, branch_id: 1 },
    { type: "processing_start", event_id: 3, turn_index: 1, branch_id: 1 },
    { type: "text_chunk", event_id: 4, content: "old-to-A", turn_index: 1, branch_id: 1 },
    { type: "processing_end", event_id: 5, turn_index: 1, branch_id: 1 },
  ]
}

/** Replay-based message list with user locator metadata for turn 1. */
function userMessages() {
  const events = originalEvents()
  const { messages } = _replayEvents([], events)
  for (const m of messages) {
    if (m.role === "user") {
      m.turnIndex = 1
      m.latestBranch = 1
    }
  }
  return { events, messages }
}

function seedInstance(chat, tab = "main") {
  chat._instanceId = "graph_1"
  chat._instanceGraphId = "graph_1"
  chat._instanceGeneration = 1
  chat.activeTab = tab
  chat.tabs = [tab]
  const { events, messages } = userMessages()
  chat.eventsByTab = { [tab]: events }
  chat.messagesByTab = { [tab]: messages }
  chat.branchViewByTab = {}
}

function branchedEvents() {
  const events = []
  const appendTurn = (turn, branch, content, response, parents) => {
    for (const [type, text] of [
      ["user_input", content],
      ["user_message", content],
      ["processing_start", ""],
      ["text_chunk", response],
      ["processing_end", ""],
    ]) {
      events.push({
        type,
        content: text,
        event_id: events.length + 1,
        turn_index: turn,
        branch_id: branch,
        parent_branch_path: parents,
      })
    }
  }
  appendTurn(1, 1, "first input", "first response", [])
  appendTurn(1, 2, "second input", "second response", [])
  appendTurn(1, 3, "third input", "original response", [])
  appendTurn(2, 2, "continue", "older continuation", [[1, 3]])
  appendTurn(2, 3, "continue", "selected continuation", [[1, 3]])
  appendTurn(3, 3, "continue again", "old descendant", [
    [1, 3],
    [2, 3],
  ])
  appendTurn(1, 4, "third input", "regenerated response", [])
  return events
}

function seedBranchedStore(ancestry = "explicit") {
  const chat = useChatStore()
  seedInstance(chat, "alice")
  const events = branchedEvents()
  if (ancestry === "legacy") {
    for (const event of events) delete event.parent_branch_path
  }
  chat.eventsByTab.alice = events
  chat.branchViewByTab = { alice: { 1: 3, 2: 3, 3: 3 }, bob: { 1: 1, 2: 1 } }
  chat._rebuildMessages("alice")
  chat._scheduleBranchResync = vi.fn()
  return { chat, events }
}

function wsFrame(type, turnIndex, branchId, extra = {}) {
  const frame = { type, source: "alice", turn_index: turnIndex, branch_id: branchId, ...extra }
  if (type === "model_recovery") {
    Object.assign(frame, {
      phase: "waiting",
      request_id: extra.request_id || `recovery-${turnIndex}`,
      sequence: 1,
      request_started_at: extra.request_started_at ?? turnIndex,
    })
  }
  return frame
}

function openSend(chat) {
  const send = vi.fn()
  chat._ws = { readyState: WebSocket.OPEN, send }
  return send
}

function transcript(chat) {
  return JSON.stringify(chat.messagesByTab.alice)
}

function turns(chat) {
  return chat.messagesByTab.alice.map((message) => message.turnIndex)
}

function lastBranchView(send) {
  return JSON.parse(send.mock.lastCall[0]).branch_view
}

describe("chat store — branch op owner/lifetime guards", () => {
  it("does not POST to a new instance when reset lands during the awaited import", async () => {
    const chat = useChatStore()
    seedInstance(chat)
    const { agentAPI } = await import("@/utils/api")
    const editSpy = vi.spyOn(agentAPI, "editMessage").mockResolvedValue({})

    // Start the op, then invalidate the instance before the dynamic import
    // resolves — the POST must never be dispatched at the new target.
    const pending = chat.editMessage(0, "edited", {
      tabId: "main",
      turnIndex: 1,
      userPosition: 0,
      latestBranch: 1,
    })
    chat.resetForRouteSwitch()
    const result = await pending

    expect(editSpy).not.toHaveBeenCalled()
    expect(result).toMatchObject({ ok: false, superseded: true })
    chat._clearBranchResyncTimers()
  })

  it("does not POST to a new instance when reset races the awaited regenerate import", async () => {
    const chat = useChatStore()
    seedInstance(chat)
    const { agentAPI } = await import("@/utils/api")
    const regenSpy = vi.spyOn(agentAPI, "regenerate").mockResolvedValue({})

    const pending = chat.regenerateLastResponse({ turnIndex: 1 })
    chat.resetForRouteSwitch()
    const result = await pending

    expect(regenSpy).not.toHaveBeenCalled()
    expect(result).toMatchObject({ ok: false, superseded: true })
    chat._clearBranchResyncTimers()
  })

  it("ignores an old edit rejection after reset+new instance and never resyncs the new target", async () => {
    const chat = useChatStore()
    seedInstance(chat)
    const { agentAPI, terrariumAPI } = await import("@/utils/api")
    let rejectEdit
    const editSpy = vi
      .spyOn(agentAPI, "editMessage")
      .mockImplementation(() => new Promise((_resolve, reject) => (rejectEdit = reject)))

    const pending = chat.editMessage(0, "edited", {
      tabId: "main",
      turnIndex: 1,
      userPosition: 0,
      latestBranch: 1,
    })
    await vi.waitFor(() => expect(editSpy).toHaveBeenCalledTimes(1))

    // New instance takes over the same tab name.
    chat.resetForRouteSwitch()
    chat._instanceId = "graph_2"
    chat._instanceGraphId = "graph_2"
    chat.activeTab = "main"
    chat.tabs = ["main"]
    chat.messagesByTab = { main: [{ id: "fresh", role: "user", content: "fresh" }] }
    chat.eventsByTab = { main: [] }
    chat.branchViewByTab = { main: {} }
    const getHistorySpy = vi.spyOn(terrariumAPI, "getHistory").mockResolvedValue({ events: [] })
    const resyncSpy = vi.spyOn(chat, "_resyncHistory").mockResolvedValue(true)

    rejectEdit(Object.assign(new Error("boom"), { code: "ECONNABORTED" }))
    const result = await pending

    expect(result).toMatchObject({ ok: false, superseded: true })
    expect(chat.messagesByTab.main).toEqual([{ id: "fresh", role: "user", content: "fresh" }])
    expect(resyncSpy).not.toHaveBeenCalled()
    expect(getHistorySpy).not.toHaveBeenCalled()
    chat._clearBranchResyncTimers()
  })

  it("late old op does not clear or roll back a newer op on the same tab", async () => {
    const chat = useChatStore()
    seedInstance(chat)
    chat._scheduleBranchResync = vi.fn()
    const { agentAPI } = await import("@/utils/api")
    let rejectA
    const editSpy = vi.spyOn(agentAPI, "editMessage")
    editSpy.mockImplementationOnce(() => new Promise((_resolve, reject) => (rejectA = reject)))
    editSpy.mockImplementationOnce(() => new Promise(() => {}))

    const pendingA = chat.editMessage(0, "A-edit", {
      tabId: "main",
      turnIndex: 1,
      userPosition: 0,
      latestBranch: 1,
    })
    await vi.waitFor(() => expect(editSpy).toHaveBeenCalledTimes(1))
    // WS idle clears op A BEFORE its HTTP response (documented store behavior).
    chat._onMessage({ type: "idle", source: "main" })
    expect(chat.branchOperationByTab.main).toBeNull()

    chat.editMessage(0, "B-edit", {
      tabId: "main",
      turnIndex: 1,
      userPosition: 0,
      latestBranch: 1,
    })
    await vi.waitFor(() => expect(editSpy).toHaveBeenCalledTimes(2))
    const operationB = chat.branchOperationByTab.main
    expect(operationB?.type).toBe("edit")

    // Old op A settles with a definite failure after B started.
    rejectA(Object.assign(new Error("conflict"), { response: { status: 409 } }))
    const resultA = await pendingA

    expect(resultA).toMatchObject({ ok: false, superseded: true })
    expect(chat.branchOperationByTab.main).toBe(operationB)
    // A must not have rolled back B's optimistic state.
    expect(JSON.stringify(chat.eventsByTab.main)).toContain("B-edit")
    expect(chat.branchViewByTab.main[1]).toBe(2)
    expect(chat.branchOperationErrorByTab.main).toBeFalsy()
    chat._clearBranchResyncTimers()
  })

  it("non-active explicit-tab op: WS idle before HTTP success still resyncs the right tab", async () => {
    const chat = useChatStore()
    seedInstance(chat)
    chat.tabs = ["main", "other"]
    chat.activeTab = "other"
    chat.messagesByTab.other = [{ id: "other-user", role: "user", content: "other draft" }]
    chat._scheduleBranchResync = vi.fn()
    const { agentAPI, terrariumAPI } = await import("@/utils/api")
    let resolveEdit
    const editSpy = vi
      .spyOn(agentAPI, "editMessage")
      .mockImplementation(() => new Promise((resolve) => (resolveEdit = resolve)))

    const pending = chat.editMessage(0, "main-edit", {
      tabId: "main",
      turnIndex: 1,
      userPosition: 0,
      latestBranch: 1,
    })
    await vi.waitFor(() => expect(editSpy).toHaveBeenCalledTimes(1))
    // Turn ends over WS before the blocking POST returns.
    chat._onMessage({ type: "idle", source: "main" })
    expect(chat.branchOperationByTab.main).toBeNull()

    const canonical = [
      ...originalEvents(),
      { type: "user_input", event_id: 6, content: "main-edit", turn_index: 1, branch_id: 2 },
      { type: "user_message", event_id: 7, content: "main-edit", turn_index: 1, branch_id: 2 },
      { type: "text_chunk", event_id: 8, content: "new-branch", turn_index: 1, branch_id: 2 },
    ]
    const getHistorySpy = vi
      .spyOn(terrariumAPI, "getHistory")
      .mockResolvedValue({ events: canonical })

    resolveEdit({ branch_id: 2, turn_index: 1 })
    const result = await pending

    expect(result.ok).toBe(true)
    expect(getHistorySpy).toHaveBeenCalledWith("graph_1", "main")
    expect(chat.branchViewByTab.main[1]).toBe(2)
    expect(chat.activeTab).toBe("other")
    expect(chat.messagesByTab.other[0].content).toBe("other draft")
    chat._clearBranchResyncTimers()
  })

  it("non-active explicit-tab error applies only to the target tab", async () => {
    const chat = useChatStore()
    seedInstance(chat)
    chat.tabs = ["main", "other"]
    chat.activeTab = "other"
    chat.messagesByTab.other = [{ id: "other-user", role: "user", content: "other draft" }]
    chat._scheduleBranchResync = vi.fn()
    const { agentAPI } = await import("@/utils/api")
    let rejectEdit
    const editSpy = vi
      .spyOn(agentAPI, "editMessage")
      .mockImplementation(() => new Promise((_resolve, reject) => (rejectEdit = reject)))

    const pending = chat.editMessage(0, "main-edit", {
      tabId: "main",
      turnIndex: 1,
      userPosition: 0,
      latestBranch: 1,
    })
    await vi.waitFor(() => expect(editSpy).toHaveBeenCalledTimes(1))
    rejectEdit(Object.assign(new Error("edit conflict"), { response: { status: 409 } }))
    const result = await pending

    expect(result.ok).toBe(false)
    expect(chat.branchOperationErrorByTab.main).toBe("edit conflict")
    expect(chat.branchOperationErrorByTab.other).toBeUndefined()
    expect(chat.messagesByTab.other[0].content).toBe("other draft")
    chat._clearBranchResyncTimers()
  })

  it("close+reopen of the same tab name supersedes the old op without touching other tabs", async () => {
    const chat = useChatStore()
    seedInstance(chat)
    chat.tabs = ["main", "other"]
    chat.activeTab = "other"
    chat.messagesByTab.other = [{ id: "other-user", role: "user", content: "other draft" }]
    chat._scheduleBranchResync = vi.fn()
    const otherMessages = chat.messagesByTab.other
    const { agentAPI } = await import("@/utils/api")
    let rejectEdit
    const editSpy = vi
      .spyOn(agentAPI, "editMessage")
      .mockImplementation(() => new Promise((_resolve, reject) => (rejectEdit = reject)))

    const pending = chat.editMessage(0, "main-edit", {
      tabId: "main",
      turnIndex: 1,
      userPosition: 0,
      latestBranch: 1,
    })
    await vi.waitFor(() => expect(editSpy).toHaveBeenCalledTimes(1))

    chat.closeTab("main")
    expect(chat._branchRequestIdByTab.main).toBeUndefined()
    expect(chat._branchRequestIdByTab.other).toBeUndefined()
    expect(chat.branchOperationByTab.main).toBeUndefined()

    // Reopen the same tab name with fresh state.
    chat.tabs = ["other", "main"]
    chat.messagesByTab.main = [{ id: "reopened", role: "user", content: "fresh" }]

    rejectEdit(Object.assign(new Error("stale"), { response: { status: 409 } }))
    const result = await pending

    expect(result).toMatchObject({ ok: false, superseded: true })
    expect(chat.messagesByTab.main).toEqual([{ id: "reopened", role: "user", content: "fresh" }])
    expect(chat.messagesByTab.other).toBe(otherMessages)
    expect(chat._branchRequestIdByTab.other).toBeUndefined()
    chat._clearBranchResyncTimers()
  })
})

describe("chat store — branch error classification (mayHaveRun)", () => {
  it("classifies 409 as definite, 502/504 as uncertain, and honours mayHaveRun", () => {
    const chat = useChatStore()
    chat.wsStatus = "closed"
    // 409 wins over a generic mayHaveRun=true hint.
    expect(chat._requestMayStillBeRunning({ response: { status: 409 }, mayHaveRun: true })).toBe(
      false,
    )
    // Gateway errors stay uncertain regardless of mayHaveRun=false.
    expect(chat._requestMayStillBeRunning({ response: { status: 502 }, mayHaveRun: false })).toBe(
      true,
    )
    expect(chat._requestMayStillBeRunning({ response: { status: 504 } })).toBe(true)
    // Lost response after dispatch: uncertain even with a closed WS.
    expect(chat._requestMayStillBeRunning({ mayHaveRun: true })).toBe(true)
    // Explicit pre-admission rejection: definite even with an open WS.
    chat.wsStatus = "open"
    expect(chat._requestMayStillBeRunning({ mayHaveRun: false })).toBe(false)
    // Absent phase falls back to the dashboard heuristics.
    expect(chat._requestMayStillBeRunning({ code: "ECONNABORTED" })).toBe(true)
    expect(chat._requestMayStillBeRunning({ code: "ETIMEDOUT" })).toBe(true)
    expect(chat._requestMayStillBeRunning({})).toBe(true)
    expect(chat._requestMayStillBeRunning({ response: { status: 500 } })).toBe(false)
  })

  it("409 with mayHaveRun=true restores only the current optimism", async () => {
    const chat = useChatStore()
    seedInstance(chat)
    const expected = JSON.parse(JSON.stringify(chat.messagesByTab.main))
    const expectedEvents = JSON.parse(JSON.stringify(chat.eventsByTab.main))
    const { agentAPI } = await import("@/utils/api")
    vi.spyOn(agentAPI, "editMessage").mockRejectedValue(
      Object.assign(new Error("conflict"), { response: { status: 409 }, mayHaveRun: true }),
    )

    const result = await chat.editMessage(0, "edited", {
      tabId: "main",
      turnIndex: 1,
      userPosition: 0,
      latestBranch: 1,
    })

    expect(result.ok).toBe(false)
    expect(result.superseded).toBeFalsy()
    expect(chat.messagesByTab.main).toEqual(expected)
    expect(chat.eventsByTab.main).toEqual(expectedEvents)
    chat._clearBranchResyncTimers()
  })

  it("502 keeps the optimism even when mayHaveRun is false", async () => {
    const chat = useChatStore()
    seedInstance(chat)
    chat._scheduleBranchResync = vi.fn()
    const { agentAPI } = await import("@/utils/api")
    vi.spyOn(agentAPI, "editMessage").mockRejectedValue(
      Object.assign(new Error("gateway"), { response: { status: 502 }, mayHaveRun: false }),
    )

    const result = await chat.editMessage(0, "edited", {
      tabId: "main",
      turnIndex: 1,
      userPosition: 0,
      latestBranch: 1,
    })

    expect(result.ok).toBe(true)
    expect(chat.branchViewByTab.main[1]).toBe(2)
    expect(JSON.stringify(chat.messagesByTab.main)).not.toContain("old-to-A")
    chat._clearBranchResyncTimers()
  })

  it("a pre-dispatch supersession restores optimism and yields the dead-view result", async () => {
    const chat = useChatStore()
    seedInstance(chat)
    const expectedMessages = JSON.parse(JSON.stringify(chat.messagesByTab.main))
    chat._scheduleBranchResync = vi.fn()
    const { agentAPI } = await import("@/utils/api")
    vi.spyOn(agentAPI, "editMessage").mockRejectedValue(
      Object.assign(new Error("superseded elsewhere"), { superseded: true, mayHaveRun: false }),
    )

    const result = await chat.editMessage(0, "edited", {
      tabId: "main",
      turnIndex: 1,
      userPosition: 0,
      latestBranch: 1,
    })

    expect(result).toMatchObject({ ok: false, superseded: true })
    expect(result.error).toBeNull()
    expect(chat.messagesByTab.main).toEqual(expectedMessages)
    expect(chat.processingByTab.main).toBe(false)
    chat._clearBranchResyncTimers()
  })

  it("does not override an explicit browsed branch when session info refreshes", () => {
    const chat = useChatStore()
    seedInstance(chat, "alice")
    expect(chat.selectBranch(1, 1, "alice")).toBe(true)
    expect(chat._browsedBranchByTab.alice).toBe(true)
    chat._handleActivity("alice", {
      activity_type: "session_info",
      branch_view: { 1: 2 },
    })
    expect(chat.branchViewByTab.alice).toEqual({ 1: 1 })
    chat._clearBranchResyncTimers()
  })

  it("does not retain incompatible old descendants while initializing an authoritative path", () => {
    const chat = useChatStore()
    seedInstance(chat, "alice")
    chat.branchViewByTab = { alice: { 1: 1, 2: 1 } }
    chat._adoptAuthoritativeBranchView("alice", { 1: 2 })
    expect(chat.branchViewByTab.alice).toEqual({ 1: 2 })
  })
})

describe("chat store — branch navigation with live descendants", () => {
  it.each(["explicit", "legacy"])(
    "prunes old descendants before continuing a regenerated response (%s ancestry)",
    async (ancestry) => {
      const { chat } = seedBranchedStore(ancestry)
      expect(turns(chat)).toEqual([1, 1, 2, 2, 3, 3])

      chat.selectBranch(1, 4, "alice")
      expect(chat.branchViewByTab.alice).toEqual({ 1: 4 })
      expect(chat.branchViewByTab.bob).toEqual({ 1: 1, 2: 1 })
      expect(turns(chat)).toEqual([1, 1])

      const send = openSend(chat)
      await chat.send("continue the regeneration")
      expect(lastBranchView(send)).toEqual({ 1: 4 })
      chat.processingByTab.alice = false

      chat.selectBranch(1, 3, "alice")
      expect(turns(chat)).toEqual([1, 1, 2, 2, 3, 3])
      expect(transcript(chat)).toContain("old descendant")
      await chat.send("continue the original")
      expect(lastBranchView(send)).toEqual({ 1: 3 })
    },
  )

  it.each([
    ["explicit", 3, 3],
    ["legacy", 3, 3],
    ["explicit", 4, 3],
    ["explicit", 4, 2],
  ])(
    "isolates a descendant stream after its ancestor selection changes (%s ancestry, branch %i, middle %i)",
    (ancestry, streamingBranch, middleBranch) => {
      const { chat } = seedBranchedStore(ancestry)
      chat.branchViewByTab.alice = { 1: 3, 2: middleBranch, 3: streamingBranch }
      chat.processingByTab.alice = true
      chat._streamingBranchByTab.alice = { turnIndex: 3, branchId: streamingBranch }

      chat.selectBranch(1, 4, "alice")
      expect(chat.branchViewByTab.alice).toEqual({ 1: 4 })
      const beforeChunk = transcript(chat)
      chat._onMessage(wsFrame("processing_start", 3, streamingBranch))
      chat._onMessage(wsFrame("text", 3, streamingBranch, { content: "off-subtree chunk" }))
      expect(transcript(chat)).toBe(beforeChunk)
      expect(chat.viewingRunningBranch).toBe(false)

      chat.selectBranch(1, 3, "alice")
      if (middleBranch !== 3) {
        const beforeWrongMiddleChunk = transcript(chat)
        chat._onMessage(wsFrame("text", 3, streamingBranch, { content: "wrong-middle chunk" }))
        expect(transcript(chat)).toBe(beforeWrongMiddleChunk)
        chat.selectBranch(2, middleBranch, "alice")
      }
      chat._onMessage(wsFrame("text", 3, streamingBranch, { content: "on-subtree chunk" }))
      expect(transcript(chat)).toContain("on-subtree chunk")
      if (streamingBranch !== 4) return

      expect(transcript(chat)).not.toContain("old descendant")
      expect(chat.branchViewByTab.alice[3]).toBe(4)
      chat.selectBranch(1, 3, "alice")
      expect(chat.branchViewByTab.alice[3]).toBe(4)
      if (middleBranch !== 3) return

      chat.selectBranch(3, 3, "alice")
      chat.selectBranch(1, 3, "alice")
      expect(chat.branchViewByTab.alice[3]).toBe(3)
      const beforeSiblingChunk = transcript(chat)
      chat._onMessage(wsFrame("text", 3, streamingBranch, { content: "wrong-sibling chunk" }))
      expect(transcript(chat)).toBe(beforeSiblingChunk)
    },
  )

  it("preserves earlier selections, compatible descendants, and uncached middle streams", async () => {
    const { chat } = seedBranchedStore()
    chat.selectBranch(2, 2, "alice")
    expect(chat.branchViewByTab.alice).toEqual({ 1: 3, 2: 2 })
    expect(turns(chat)).toEqual([1, 1, 2, 2])
    expect(transcript(chat)).toContain("older continuation")
    expect(transcript(chat)).not.toContain("old descendant")

    chat.selectBranch(1, 3, "alice")
    expect(chat.branchViewByTab.alice).toEqual({ 1: 3, 2: 2 })
    expect(transcript(chat)).not.toContain("selected continuation")

    chat.branchViewByTab.alice = { 1: 3, 3: 3 }
    chat._streamingBranchByTab.alice = { turnIndex: 2, branchId: 4 }
    chat.selectBranch(1, 3, "alice")
    expect(chat.branchViewByTab.alice).toEqual({ 1: 3, 2: 4 })
    expect(transcript(chat)).not.toContain("old descendant")
    const send = openSend(chat)
    await chat.send("continue the live middle response")
    expect(lastBranchView(send)).toEqual({ 1: 3, 2: 4 })
  })

  it("clears browsed markers when unbinding so a later same-name tab can restore", () => {
    const chat = useChatStore()
    seedInstance(chat, "alice")
    chat.selectBranch(1, 1, "alice")
    chat.unbindFromInstance()
    expect(chat._browsedBranchByTab).toEqual({})
    chat.activeTab = "alice"
    chat.tabs = ["alice"]
    chat._adoptAuthoritativeBranchView("alice", { 1: 2 })
    expect(chat.branchViewByTab.alice).toEqual({ 1: 2 })
  })
})

describe("chat store — send ancestry freeze until streaming coordinates arrive", () => {
  it.each([
    ["explicit", "processing_start"],
    ["legacy", "processing_start"],
    ["explicit", "model_recovery"],
    ["paged-view", "processing_start"],
    ["paged-path", "processing_start"],
    ["retry", "processing_start"],
    ["paged-retry", "model_recovery"],
    ["queued", "processing_start"],
  ])("captures send ancestry before delayed coordinates (%s, %s)", async (mode, startKind) => {
    const { chat, events } = seedBranchedStore(mode === "legacy" ? "legacy" : "explicit")
    const send = openSend(chat)
    if (mode.startsWith("paged")) {
      chat.eventsByTab.alice = events.filter((event) => event.turn_index > 1)
      if (mode !== "paged-view") chat.branchViewByTab.alice = { 2: 3, 3: 3 }
    }
    if (mode.endsWith("retry")) {
      await chat.send("failed attempt")
      chat._onMessage(wsFrame(startKind, 4, 2))
      if (mode === "paged-retry") {
        chat.selectBranch(2, 3, "alice")
        expect(chat.branchViewByTab.alice[4]).toBe(2)
      }
      chat._onMessage({ type: "error", source: "alice", content: "failed" })
    }
    const runningTurn = mode.endsWith("retry") ? 5 : 4
    await chat.send("continue the original")
    expect(lastBranchView(send)).toEqual(chat.branchViewByTab.alice)
    chat.eventsByTab.alice = events
    chat.selectBranch(1, 4, "alice")
    if (mode === "queued") await chat.send("queued follow-up")
    expect(transcript(chat)).not.toContain("old descendant")
    const start = wsFrame(startKind, runningTurn, 1)
    let before = transcript(chat)
    chat._onMessage(start)
    if (mode === "queued") before = transcript(chat)
    chat._onMessage(wsFrame("text", runningTurn, 1, { content: "off-subtree chunk" }))
    expect(transcript(chat)).toBe(before)
    expect(chat.viewingRunningBranch).toBe(false)
    chat._onMessage(start)
    chat._onMessage(wsFrame("text", runningTurn, 1, { content: "off-subtree chunk" }))
    expect(transcript(chat)).toBe(before)
    chat.selectBranch(1, 3, "alice")
    if (mode === "paged-retry") {
      chat.selectBranch(2, 3, "alice")
      chat.selectBranch(4, 1, "alice")
      const beforeWrongTail = transcript(chat)
      chat._onMessage(wsFrame("text", runningTurn, 1, { content: "wrong-tail chunk" }))
      expect(transcript(chat)).toBe(beforeWrongTail)
      chat.selectBranch(4, 2, "alice")
    }
    chat._onMessage(wsFrame("text", runningTurn, 1, { content: "on-subtree chunk" }))
    expect(transcript(chat)).toContain("on-subtree chunk")
  })

  it.each([
    ["sparse", { 1: 4 }, 2, 4],
    ["middle", { 1: 3, 2: 2 }, 3, 4],
  ])("snapshots %s send ancestry without stale descendants", async (label, view, turn, branch) => {
    const { chat } = seedBranchedStore()
    if (label === "middle") chat.selectBranch(2, 2, "alice")
    else chat.selectBranch(1, 4, "alice")
    expect(chat.branchViewByTab.alice).toEqual(view)
    const send = openSend(chat)
    await chat.send(label === "middle" ? "continue from middle 2" : "continue the regeneration")
    expect(lastBranchView(send)).toEqual(view)
    chat.selectBranch(1, label === "middle" ? 4 : 3, "alice")
    const beforeWrong = transcript(chat)
    chat._onMessage(wsFrame("processing_start", turn, branch))
    chat._onMessage(wsFrame("text", turn, branch, { content: "wrong-middle chunk" }))
    expect(transcript(chat)).toBe(beforeWrong)
    expect(chat.viewingRunningBranch).toBe(false)
    if (label === "middle") {
      chat.selectBranch(1, 3, "alice")
      const beforeDefaultMiddle = transcript(chat)
      chat._onMessage(wsFrame("text", turn, branch, { content: "default-middle chunk" }))
      expect(transcript(chat)).toBe(beforeDefaultMiddle)
      chat.selectBranch(2, 2, "alice")
    } else {
      chat.selectBranch(1, 4, "alice")
    }
    chat._onMessage(wsFrame("text", turn, branch, { content: "on-path chunk" }))
    expect(transcript(chat)).toContain("on-path chunk")
    if (label === "sparse") expect(transcript(chat)).not.toContain("old descendant")
  })

  it("accepts a delayed start without a branch switch", async () => {
    const { chat } = seedBranchedStore()
    openSend(chat)
    await chat.send("continue the original")
    chat._onMessage(wsFrame("processing_start", 4, 1))
    expect(chat.viewingRunningBranch).toBe(true)
    chat._onMessage(wsFrame("text", 4, 1, { content: "control chunk" }))
    expect(transcript(chat)).toContain("control chunk")
  })

  it.each(["processing_end", "idle", "error", "reset", "unbind", "close", "staleop"])(
    "clears pending ancestry on %s",
    async (action) => {
      const { chat } = seedBranchedStore()
      openSend(chat)
      await chat.send("continue the original")
      expect(chat._pendingBranchPathByTab.alice).toBeTruthy()
      if (action === "reset") chat.resetForRouteSwitch()
      else if (action === "unbind") chat.unbindFromInstance()
      else if (action === "close") chat.closeTab("alice")
      else if (action === "staleop")
        chat._dropStaleBranchOp("alice", { expectedBranchByTurn: { 4: 1 } })
      else chat._onMessage({ type: action, source: "alice", content: "failed" })
      expect(chat._pendingBranchPathByTab.alice).toBeUndefined()
    },
  )
})
