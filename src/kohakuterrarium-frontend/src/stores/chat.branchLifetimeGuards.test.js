import { createPinia, setActivePinia } from "pinia"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { _replayEvents, useChatStore } from "./chat.js"

beforeEach(() => {
  setActivePinia(createPinia())
})

afterEach(() => {
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

  it.each(["explicit", "legacy"])(
    "prunes old descendants before continuing a regenerated response (%s ancestry)",
    async (ancestry) => {
      const chat = useChatStore()
      seedInstance(chat, "alice")
      const events = branchedEvents()
      if (ancestry === "legacy") {
        for (const event of events) delete event.parent_branch_path
      }
      chat.eventsByTab.alice = events
      chat.branchViewByTab = { alice: { 1: 3, 2: 3, 3: 3 }, bob: { 1: 1, 2: 1 } }
      chat._rebuildMessages("alice")
      expect(chat.messagesByTab.alice.map((message) => message.turnIndex)).toEqual([
        1, 1, 2, 2, 3, 3,
      ])

      expect(chat.selectBranch(1, 4, "alice")).toBe(true)
      expect(chat.branchViewByTab.alice).toEqual({ 1: 4 })
      expect(chat.branchViewByTab.bob).toEqual({ 1: 1, 2: 1 })
      expect(chat.messagesByTab.alice.map((message) => message.turnIndex)).toEqual([1, 1])
      expect(chat.messagesByTab.alice[0]).toMatchObject({
        userGroupCount: 3,
        currentUserGroupIdx: 2,
      })
      expect(chat.messagesByTab.alice[1]).toMatchObject({
        assistantBranches: [3, 4],
        currentAssistantIdx: 1,
      })

      const send = vi.fn()
      chat._ws = { readyState: WebSocket.OPEN, send }
      await chat.send("continue the regeneration")
      expect(JSON.parse(send.mock.calls[0][0])).toMatchObject({
        type: "input",
        target: "alice",
        branch_view: { 1: 4 },
      })
      chat.processingByTab.alice = false

      expect(chat.selectBranch(1, 3, "alice")).toBe(true)
      expect(chat.messagesByTab.alice.map((message) => message.turnIndex)).toEqual([
        1, 1, 2, 2, 3, 3,
      ])
      expect(JSON.stringify(chat.messagesByTab.alice)).toContain("old descendant")
      await chat.send("continue the original")
      expect(JSON.parse(send.mock.calls[1][0]).branch_view).toEqual({ 1: 3 })
      chat._clearBranchResyncTimers()
    },
  )

  it.each([
    ["explicit", 3, 3],
    ["legacy", 3, 3],
    ["explicit", 4, 3],
    ["legacy", 4, 3],
    ["explicit", 4, 2],
    ["legacy", 4, 2],
  ])(
    "isolates a descendant stream after its ancestor selection changes (%s ancestry, branch %i, middle %i)",
    (ancestry, streamingBranch, middleBranch) => {
      const chat = useChatStore()
      seedInstance(chat, "alice")
      const events = branchedEvents()
      if (ancestry === "legacy") {
        for (const event of events) delete event.parent_branch_path
      }
      chat.eventsByTab.alice = events
      chat.branchViewByTab.alice = { 1: 3, 2: middleBranch, 3: streamingBranch }
      chat.processingByTab.alice = true
      chat._streamingBranchByTab.alice = { turnIndex: 3, branchId: streamingBranch }
      chat._scheduleBranchResync = vi.fn()

      expect(chat.selectBranch(1, 4, "alice")).toBe(true)
      expect(chat.branchViewByTab.alice).toEqual({ 1: 4 })
      const beforeChunk = JSON.stringify(chat.messagesByTab.alice)
      chat._onMessage({
        type: "processing_start",
        source: "alice",
        turn_index: 3,
        branch_id: streamingBranch,
      })
      chat._onMessage({
        type: "text",
        source: "alice",
        content: "off-subtree chunk",
        turn_index: 3,
        branch_id: streamingBranch,
      })
      expect(JSON.stringify(chat.messagesByTab.alice)).toBe(beforeChunk)
      expect(chat.viewingRunningBranch).toBe(false)
      expect(chat.processingByTab.alice).toBe(true)

      expect(chat.selectBranch(1, 3, "alice")).toBe(true)
      expect(chat.viewingRunningBranch).toBe(middleBranch === 3)
      if (middleBranch !== 3) {
        const beforeWrongMiddleChunk = JSON.stringify(chat.messagesByTab.alice)
        chat._onMessage({
          type: "text",
          source: "alice",
          content: "wrong-middle chunk",
          turn_index: 3,
          branch_id: streamingBranch,
        })
        expect(JSON.stringify(chat.messagesByTab.alice)).toBe(beforeWrongMiddleChunk)
        expect(chat.selectBranch(2, middleBranch, "alice")).toBe(true)
        expect(chat.viewingRunningBranch).toBe(true)
      }
      chat._onMessage({
        type: "text",
        source: "alice",
        content: "on-subtree chunk",
        turn_index: 3,
        branch_id: streamingBranch,
      })
      expect(JSON.stringify(chat.messagesByTab.alice)).toContain("on-subtree chunk")
      if (streamingBranch === 4) {
        expect(JSON.stringify(chat.messagesByTab.alice)).not.toContain("old descendant")
        expect(chat.branchViewByTab.alice[3]).toBe(4)
        expect(chat.selectBranch(1, 3, "alice")).toBe(true)
        expect(chat.branchViewByTab.alice[3]).toBe(4)
        expect(JSON.stringify(chat.messagesByTab.alice)).not.toContain("old descendant")
        if (middleBranch === 3) {
          expect(chat.selectBranch(3, 3, "alice")).toBe(true)
          expect(chat.selectBranch(1, 3, "alice")).toBe(true)
          expect(chat.branchViewByTab.alice[3]).toBe(3)
          expect(chat.viewingRunningBranch).toBe(false)
          const beforeSiblingChunk = JSON.stringify(chat.messagesByTab.alice)
          chat._onMessage({
            type: "text",
            source: "alice",
            content: "wrong-sibling chunk",
            turn_index: 3,
            branch_id: streamingBranch,
          })
          expect(JSON.stringify(chat.messagesByTab.alice)).toBe(beforeSiblingChunk)
        }
      }
      chat._clearBranchResyncTimers()
    },
  )

  it("preserves earlier selections and resets descendants when switching a middle turn", () => {
    const chat = useChatStore()
    seedInstance(chat, "alice")
    chat.eventsByTab.alice = branchedEvents()
    chat.branchViewByTab.alice = { 1: 3, 2: 3, 3: 3 }

    expect(chat.selectBranch(2, 2, "alice")).toBe(true)
    expect(chat.branchViewByTab.alice).toEqual({ 1: 3, 2: 2 })
    expect(chat.messagesByTab.alice.map((message) => message.turnIndex)).toEqual([1, 1, 2, 2])
    expect(JSON.stringify(chat.messagesByTab.alice)).toContain("older continuation")
    expect(JSON.stringify(chat.messagesByTab.alice)).not.toContain("old descendant")
    chat._clearBranchResyncTimers()
  })

  it("prunes descendants of the cached sibling when restoring an uncached middle stream", async () => {
    const chat = useChatStore()
    seedInstance(chat, "alice")
    chat.eventsByTab.alice = branchedEvents()
    chat.branchViewByTab.alice = { 1: 3, 3: 3 }
    chat._streamingBranchByTab.alice = { turnIndex: 2, branchId: 4 }
    chat._scheduleBranchResync = vi.fn()

    expect(chat.selectBranch(1, 3, "alice")).toBe(true)
    expect(chat.branchViewByTab.alice).toEqual({ 1: 3, 2: 4 })
    expect(JSON.stringify(chat.messagesByTab.alice)).not.toContain("old descendant")
    const send = vi.fn()
    chat._ws = { readyState: WebSocket.OPEN, send }
    await chat.send("continue the live middle response")
    expect(JSON.parse(send.mock.calls[0][0]).branch_view).toEqual({ 1: 3, 2: 4 })
    chat._clearBranchResyncTimers()
  })

  it("keeps compatible descendant choices when selecting the same early branch", () => {
    const chat = useChatStore()
    seedInstance(chat, "alice")
    chat.eventsByTab.alice = branchedEvents()
    chat.branchViewByTab.alice = { 1: 3, 2: 2 }

    expect(chat.selectBranch(1, 3, "alice")).toBe(true)
    expect(chat.branchViewByTab.alice).toEqual({ 1: 3, 2: 2 })
    expect(JSON.stringify(chat.messagesByTab.alice)).toContain("older continuation")
    expect(JSON.stringify(chat.messagesByTab.alice)).not.toContain("selected continuation")
    chat._clearBranchResyncTimers()
  })

  it("clears browsed markers when unbinding so a later same-name tab can restore", () => {
    const chat = useChatStore()
    seedInstance(chat, "alice")
    expect(chat.selectBranch(1, 1, "alice")).toBe(true)
    chat.unbindFromInstance()
    expect(chat._browsedBranchByTab).toEqual({})
    chat.activeTab = "alice"
    chat.tabs = ["alice"]
    chat._adoptAuthoritativeBranchView("alice", { 1: 2 })
    expect(chat.branchViewByTab.alice).toEqual({ 1: 2 })
  })
})

describe("chat store — send ancestry freeze until streaming coordinates arrive", () => {
  function seedBranchedTab(chat, ancestry) {
    seedInstance(chat, "alice")
    const events = branchedEvents()
    if (ancestry === "legacy") {
      for (const event of events) delete event.parent_branch_path
    }
    chat.eventsByTab.alice = events
    chat.branchViewByTab = { alice: { 1: 3, 2: 3, 3: 3 }, bob: { 1: 1, 2: 1 } }
    chat._rebuildMessages("alice")
    chat._scheduleBranchResync = vi.fn()
    return events
  }

  function startFrame(kind, turnIndex, branchId) {
    if (kind === "model_recovery") {
      return {
        type: "model_recovery",
        source: "alice",
        phase: "waiting",
        turn_index: turnIndex,
        branch_id: branchId,
        request_id: `recovery-${turnIndex}`,
        sequence: 1,
        request_started_at: turnIndex,
      }
    }
    return {
      type: "processing_start",
      source: "alice",
      turn_index: turnIndex,
      branch_id: branchId,
    }
  }

  it.each(
    ["explicit", "legacy", "paged-view", "paged-path", "retry", "paged-retry"].flatMap((ancestry) =>
      ["processing_start", "model_recovery"].map((kind) => [ancestry, kind]),
    ),
  )(
    "captures send ancestry before delayed coordinates (%s ancestry, %s)",
    async (ancestry, startKind) => {
      const chat = useChatStore()
      const allEvents = seedBranchedTab(chat, ancestry)
      const send = vi.fn()
      chat._ws = { readyState: WebSocket.OPEN, send }
      if (ancestry.startsWith("paged")) {
        chat.eventsByTab.alice = allEvents.filter((event) => event.turn_index > 1)
        if (ancestry !== "paged-view") chat.branchViewByTab.alice = { 2: 3, 3: 3 }
      }
      const expectedPath = [
        [1, 3],
        [2, 3],
        [3, 3],
      ]
      if (ancestry.endsWith("retry")) {
        await chat.send("failed attempt")
        chat._onMessage(startFrame(startKind, 4, 2))
        if (ancestry === "paged-retry") {
          expect(chat.selectBranch(2, 3, "alice")).toBe(true)
          expect(chat.branchViewByTab.alice[4]).toBe(2)
          expectedPath.push([4, 2])
        }
        chat._onMessage({ type: "error", source: "alice", content: "failed" })
      }
      const runningTurn = ancestry.endsWith("retry") ? 5 : 4
      await chat.send("continue the original")
      expect(JSON.parse(send.mock.lastCall[0]).branch_view).toEqual(chat.branchViewByTab.alice)
      chat.eventsByTab.alice = allEvents
      expect(chat.processingByTab.alice).toBe(true)
      if (!ancestry.endsWith("retry")) expect(chat._streamingBranchByTab.alice).toBeUndefined()

      expect(chat.selectBranch(1, 4, "alice")).toBe(true)
      expect(chat.branchViewByTab.alice).toEqual({ 1: 4 })
      expect(chat.branchViewByTab.bob).toEqual({ 1: 1, 2: 1 })
      const beforeChunk = JSON.stringify(chat.messagesByTab.alice)
      expect(beforeChunk).not.toContain("old descendant")
      expect(chat.processingByTab.alice).toBe(true)

      chat._onMessage(startFrame(startKind, runningTurn, 1))
      expect(chat._streamingBranchByTab.alice).toMatchObject({
        turnIndex: runningTurn,
        branchId: 1,
      })
      expect(chat._pendingBranchPathByTab.alice).toBeUndefined()
      chat._onMessage({
        type: "text",
        source: "alice",
        content: "off-subtree chunk",
        turn_index: runningTurn,
        branch_id: 1,
      })
      expect(JSON.stringify(chat.messagesByTab.alice)).toBe(beforeChunk)
      expect(chat.viewingRunningBranch).toBe(false)
      expect(chat.processingByTab.alice).toBe(true)

      chat._onMessage(startFrame(startKind, runningTurn, 1))
      expect(chat._streamingBranchByTab.alice.parentBranchPath).toEqual(expectedPath)

      expect(chat.selectBranch(1, 3, "alice")).toBe(true)
      expect(chat.viewingRunningBranch).toBe(true)
      chat._onMessage({
        type: "text",
        source: "alice",
        content: "on-subtree chunk",
        turn_index: runningTurn,
        branch_id: 1,
      })
      expect(JSON.stringify(chat.messagesByTab.alice)).toContain("on-subtree chunk")
      chat._clearBranchResyncTimers()
    },
  )

  it.each(["explicit", "legacy"])(
    "snapshots sparse view{1:4} without stale cached descendants (%s ancestry)",
    async (ancestry) => {
      const chat = useChatStore()
      seedBranchedTab(chat, ancestry)
      expect(chat.selectBranch(1, 4, "alice")).toBe(true)
      expect(chat.branchViewByTab.alice).toEqual({ 1: 4 })
      const send = vi.fn()
      chat._ws = { readyState: WebSocket.OPEN, send }

      await chat.send("continue the regeneration")
      expect(JSON.parse(send.mock.calls[0][0]).branch_view).toEqual({ 1: 4 })
      expect(chat._pendingBranchPathByTab.alice).toEqual([[1, 4]])

      expect(chat.selectBranch(1, 3, "alice")).toBe(true)
      expect(chat.messagesByTab.alice.map((message) => message.turnIndex)).toEqual([
        1, 1, 2, 2, 3, 3,
      ])
      const beforeWrongSubtree = JSON.stringify(chat.messagesByTab.alice)
      chat._onMessage({
        type: "processing_start",
        source: "alice",
        turn_index: 2,
        branch_id: 4,
      })
      chat._onMessage({
        type: "text",
        source: "alice",
        content: "stale-descendant chunk",
        turn_index: 2,
        branch_id: 4,
      })
      expect(JSON.stringify(chat.messagesByTab.alice)).toBe(beforeWrongSubtree)
      expect(chat.viewingRunningBranch).toBe(false)

      expect(chat.selectBranch(1, 4, "alice")).toBe(true)
      expect(chat.viewingRunningBranch).toBe(true)
      chat._onMessage({
        type: "text",
        source: "alice",
        content: "on-sparse-view chunk",
        turn_index: 2,
        branch_id: 4,
      })
      expect(JSON.stringify(chat.messagesByTab.alice)).toContain("on-sparse-view chunk")
      expect(JSON.stringify(chat.messagesByTab.alice)).not.toContain("old descendant")
      chat._clearBranchResyncTimers()
    },
  )

  it.each(["explicit", "legacy"])(
    "keeps a non-latest middle ancestor until it is reselected (%s ancestry)",
    async (ancestry) => {
      const chat = useChatStore()
      seedBranchedTab(chat, ancestry)
      expect(chat.selectBranch(2, 2, "alice")).toBe(true)
      expect(chat.branchViewByTab.alice).toEqual({ 1: 3, 2: 2 })
      const send = vi.fn()
      chat._ws = { readyState: WebSocket.OPEN, send }

      await chat.send("continue from middle 2")
      expect(JSON.parse(send.mock.calls[0][0]).branch_view).toEqual({ 1: 3, 2: 2 })
      expect(chat._pendingBranchPathByTab.alice).toEqual([
        [1, 3],
        [2, 2],
      ])

      expect(chat.selectBranch(1, 4, "alice")).toBe(true)
      expect(chat.branchViewByTab.alice).toEqual({ 1: 4 })
      chat._onMessage({
        type: "processing_start",
        source: "alice",
        turn_index: 3,
        branch_id: 4,
      })
      expect(chat._streamingBranchByTab.alice.parentBranchPath).toEqual([
        [1, 3],
        [2, 2],
      ])
      const beforeWrong = JSON.stringify(chat.messagesByTab.alice)
      chat._onMessage({
        type: "text",
        source: "alice",
        content: "wrong-middle chunk",
        turn_index: 3,
        branch_id: 4,
      })
      expect(JSON.stringify(chat.messagesByTab.alice)).toBe(beforeWrong)
      expect(chat.viewingRunningBranch).toBe(false)

      expect(chat.selectBranch(1, 3, "alice")).toBe(true)
      expect(chat.branchViewByTab.alice[2] === 2).toBe(false)
      expect(chat.viewingRunningBranch).toBe(false)
      const beforeDefaultMiddle = JSON.stringify(chat.messagesByTab.alice)
      chat._onMessage({
        type: "text",
        source: "alice",
        content: "default-middle chunk",
        turn_index: 3,
        branch_id: 4,
      })
      expect(JSON.stringify(chat.messagesByTab.alice)).toBe(beforeDefaultMiddle)

      expect(chat.selectBranch(2, 2, "alice")).toBe(true)
      expect(chat.viewingRunningBranch).toBe(true)
      chat._onMessage({
        type: "text",
        source: "alice",
        content: "middle-2 chunk",
        turn_index: 3,
        branch_id: 4,
      })
      expect(JSON.stringify(chat.messagesByTab.alice)).toContain("middle-2 chunk")
      chat._clearBranchResyncTimers()
    },
  )

  it("does not let a queued follow-up overwrite the in-flight snapshot", async () => {
    const chat = useChatStore()
    seedBranchedTab(chat, "explicit")
    const send = vi.fn()
    chat._ws = { readyState: WebSocket.OPEN, send }

    await chat.send("continue the original")
    expect(JSON.parse(send.mock.calls[0][0]).branch_view).toEqual({ 1: 3, 2: 3, 3: 3 })
    expect(chat._pendingBranchPathByTab.alice).toEqual([
      [1, 3],
      [2, 3],
      [3, 3],
    ])

    expect(chat.selectBranch(1, 4, "alice")).toBe(true)
    await chat.send("queued follow-up")
    expect(chat.queuedMessagesByTab.alice).toHaveLength(1)
    expect(JSON.parse(send.mock.calls[1][0]).branch_view).toEqual({ 1: 4 })
    expect(chat._pendingBranchPathByTab.alice).toEqual([
      [1, 3],
      [2, 3],
      [3, 3],
    ])

    chat._onMessage({
      type: "processing_start",
      source: "alice",
      turn_index: 4,
      branch_id: 1,
    })
    expect(chat._streamingBranchByTab.alice.parentBranchPath).toEqual([
      [1, 3],
      [2, 3],
      [3, 3],
    ])
    expect(chat.viewingRunningBranch).toBe(false)
    chat._clearBranchResyncTimers()
  })

  it("accepts a delayed start without a branch switch", async () => {
    const chat = useChatStore()
    seedBranchedTab(chat, "explicit")
    const send = vi.fn()
    chat._ws = { readyState: WebSocket.OPEN, send }

    await chat.send("continue the original")
    expect(JSON.parse(send.mock.calls[0][0]).branch_view).toEqual({ 1: 3, 2: 3, 3: 3 })
    chat._onMessage({
      type: "processing_start",
      source: "alice",
      turn_index: 4,
      branch_id: 1,
    })
    expect(chat.viewingRunningBranch).toBe(true)
    chat._onMessage({
      type: "text",
      source: "alice",
      content: "control chunk",
      turn_index: 4,
      branch_id: 1,
    })
    expect(JSON.stringify(chat.messagesByTab.alice)).toContain("control chunk")
    chat._clearBranchResyncTimers()
  })

  it.each(["processing_end", "idle", "error"])(
    "clears pending ancestry on %s so the next turn does not inherit it",
    async (terminal) => {
      const chat = useChatStore()
      seedBranchedTab(chat, "explicit")
      const send = vi.fn()
      chat._ws = { readyState: WebSocket.OPEN, send }

      await chat.send("continue the original")
      expect(chat._pendingBranchPathByTab.alice).toEqual([
        [1, 3],
        [2, 3],
        [3, 3],
      ])
      chat._onMessage({ type: terminal, source: "alice", content: "failed" })
      expect(chat._pendingBranchPathByTab.alice).toBeUndefined()
      expect(chat._streamingBranchByTab.alice).toBeUndefined()

      expect(chat.selectBranch(1, 4, "alice")).toBe(true)
      await chat.send("next turn")
      expect(JSON.parse(send.mock.calls[1][0]).branch_view).toEqual({ 1: 4 })
      expect(chat._pendingBranchPathByTab.alice).toEqual([[1, 4]])
      chat._onMessage({
        type: "processing_start",
        source: "alice",
        turn_index: 2,
        branch_id: 5,
      })
      expect(chat._streamingBranchByTab.alice.parentBranchPath).toEqual([[1, 4]])
      chat._clearBranchResyncTimers()
    },
  )

  it("does not leak pending ancestry across reset, unbind, tab close, or stale-op drop", async () => {
    const chat = useChatStore()
    seedBranchedTab(chat, "explicit")
    const send = vi.fn()
    chat._ws = { readyState: WebSocket.OPEN, send }

    await chat.send("continue the original")
    expect(chat._pendingBranchPathByTab.alice).toBeTruthy()
    chat.closeTab("alice")
    expect(chat._pendingBranchPathByTab.alice).toBeUndefined()

    seedBranchedTab(chat, "explicit")
    chat._ws = { readyState: WebSocket.OPEN, send }
    await chat.send("continue the original")
    chat.unbindFromInstance()
    expect(chat._pendingBranchPathByTab).toEqual({})

    seedBranchedTab(chat, "explicit")
    chat._ws = { readyState: WebSocket.OPEN, send }
    await chat.send("continue the original")
    chat.resetForRouteSwitch()
    expect(chat._pendingBranchPathByTab).toEqual({})

    seedBranchedTab(chat, "explicit")
    chat._ws = { readyState: WebSocket.OPEN, send }
    await chat.send("continue the original")
    expect(chat._pendingBranchPathByTab.alice).toBeTruthy()
    chat._dropStaleBranchOp("alice", { expectedBranchByTurn: { 4: 1 } })
    expect(chat._pendingBranchPathByTab.alice).toBeUndefined()
    chat._clearBranchResyncTimers()
  })
})
