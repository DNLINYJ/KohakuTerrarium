import { createPinia, setActivePinia } from "pinia"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { agentAPI, terrariumAPI } from "@/utils/api"

import { _replayEvents, useChatStore } from "./chat.js"

const stores = []
beforeEach(() => {
  setActivePinia(createPinia())
})
afterEach(() => {
  for (const chat of stores.splice(0)) chat._clearBranchResyncTimers()
  vi.restoreAllMocks()
})

const IMAGE_A = {
  type: "image_url",
  image_url: { url: "data:image/png;base64,aaa", detail: "low" },
}
const IMAGE_B = {
  type: "image_url",
  image_url: { url: "data:image/png;base64,bbb", detail: "low" },
}

function seedTab(overrides = {}) {
  const chat = useChatStore()
  chat._instanceId = "g1"
  chat._instanceGraphId = "g1"
  chat._instanceGeneration = 1
  chat.tabs = ["main"]
  chat.activeTab = "main"
  chat.messagesByTab = { main: [] }
  chat.queuedMessagesByTab = { main: [] }
  chat.eventsByTab = { main: [] }
  Object.assign(chat, overrides)
  stores.push(chat)
  return chat
}

function seedBranchHistory(content = "hello") {
  const events = [
    { type: "user_input", event_id: 1, content, turn_index: 1, branch_id: 1 },
    { type: "user_message", event_id: 2, content, turn_index: 1, branch_id: 1 },
    { type: "processing_start", event_id: 3, turn_index: 1, branch_id: 1 },
    {
      type: "text_chunk",
      event_id: 4,
      content: `${content}-answer`,
      turn_index: 1,
      branch_id: 1,
    },
    { type: "processing_end", event_id: 5, turn_index: 1, branch_id: 1 },
  ]
  const chat = seedTab()
  chat.eventsByTab.main = events
  chat.messagesByTab.main = _replayEvents([], events).messages
  return chat
}

describe("multimodal identity — user_input echo", () => {
  it("reconciles the optimistic row in place when pending_id matches", () => {
    const chat = seedTab()
    const eventId = "c_send_1"
    chat.messagesByTab.main = [
      {
        id: "u_1",
        role: "user",
        content: "",
        contentParts: [IMAGE_A],
        eventId,
        timestamp: "t0",
      },
    ]

    chat._handleUserInput("main", {
      content: [IMAGE_A],
      pending_id: eventId,
      event_id: 41,
      turn_index: 1,
      branch_id: 1,
    })

    expect(chat.messagesByTab.main).toHaveLength(1)
    expect(chat.messagesByTab.main[0]).toMatchObject({
      id: "u_1",
      eventId,
      contentParts: [IMAGE_A],
    })
  })

  it("appends distinct pending_ids even when image-only content is identical", () => {
    const chat = seedTab()
    chat.messagesByTab.main = [
      {
        id: "u_1",
        role: "user",
        content: "",
        contentParts: [IMAGE_A],
        eventId: "c_send_1",
      },
    ]

    chat._handleUserInput("main", {
      content: [IMAGE_A],
      pending_id: "c_send_2",
      event_id: 42,
      turn_index: 2,
      branch_id: 1,
    })

    expect(chat.messagesByTab.main).toHaveLength(2)
    expect(chat.messagesByTab.main.map((m) => m.eventId)).toEqual(["c_send_1", "c_send_2"])
  })

  it("does not drop a second identical-text send that carries a different pending_id", () => {
    const chat = seedTab()
    chat.messagesByTab.main = [
      {
        id: "u_1",
        role: "user",
        content: "ping",
        contentParts: [{ type: "text", text: "ping" }],
        eventId: "c_send_1",
      },
    ]

    chat._handleUserInput("main", {
      content: [{ type: "text", text: "ping" }],
      pending_id: "c_send_2",
      event_id: 43,
    })

    expect(chat.messagesByTab.main).toHaveLength(2)
    expect(chat.messagesByTab.main.map((m) => m.eventId)).toEqual(["c_send_1", "c_send_2"])
  })

  it("repeated echo of the same pending_id does not duplicate the row", () => {
    const chat = seedTab()
    chat.messagesByTab.main = [
      {
        id: "u_1",
        role: "user",
        content: "hello",
        contentParts: [{ type: "text", text: "hello" }],
        eventId: "c_send_1",
      },
    ]

    const echo = {
      content: [{ type: "text", text: "hello" }],
      pending_id: "c_send_1",
      event_id: 45,
      turn_index: 1,
      branch_id: 1,
    }
    chat._handleUserInput("main", echo)
    chat._handleUserInput("main", echo)

    expect(chat.messagesByTab.main).toHaveLength(1)
    expect(chat.messagesByTab.main[0].eventId).toBe("c_send_1")
  })

  it("replaces optimistic parts with the authoritative full content for the same pending_id", () => {
    const chat = seedTab()
    chat.messagesByTab.main = [
      {
        id: "u_1",
        role: "user",
        content: "caption",
        contentParts: [{ type: "text", text: "caption" }],
        eventId: "c_send_1",
      },
    ]

    chat._handleUserInput("main", {
      content: [{ type: "text", text: "caption" }, IMAGE_A],
      pending_id: "c_send_1",
      event_id: 46,
    })

    expect(chat.messagesByTab.main).toHaveLength(1)
    expect(chat.messagesByTab.main[0].contentParts).toEqual([
      { type: "text", text: "caption" },
      IMAGE_A,
    ])
  })

  it("never falls back to content matching when a supplied pending_id is not found", () => {
    const chat = seedTab()
    chat.messagesByTab.main = [
      {
        id: "u_other",
        role: "user",
        content: "same",
        contentParts: [{ type: "text", text: "same" }],
        eventId: "c_other",
      },
    ]

    chat._handleUserInput("main", {
      content: [{ type: "text", text: "same" }],
      pending_id: "c_missing",
      event_id: 47,
    })

    expect(chat.messagesByTab.main).toHaveLength(2)
    expect(chat.messagesByTab.main.map((m) => m.eventId)).toEqual(["c_other", "c_missing"])
  })

  it("legacy identity-poor fallback only matches a unique complete payload candidate", () => {
    const chat = seedTab()
    chat.messagesByTab.main = [
      {
        id: "u_1",
        role: "user",
        content: "same",
        contentParts: [{ type: "text", text: "same" }],
        eventId: "c_opt_1",
        _inputEchoPending: true,
      },
    ]

    chat._handleUserInput("main", {
      content: [{ type: "text", text: "same" }],
      event_id: 48,
    })

    expect(chat.messagesByTab.main).toHaveLength(1)
    expect(chat.messagesByTab.main[0].id).toBe("u_1")
  })

  it("legacy fallback cannot collapse two optimistic rows that share the same payload", () => {
    const chat = seedTab()
    chat.messagesByTab.main = [
      {
        id: "u_1",
        role: "user",
        content: "same",
        contentParts: [{ type: "text", text: "same" }],
        eventId: "c_opt_1",
        _inputEchoPending: true,
      },
      {
        id: "u_2",
        role: "user",
        content: "same",
        contentParts: [{ type: "text", text: "same" }],
        eventId: "c_opt_2",
        _inputEchoPending: true,
      },
    ]

    chat._handleUserInput("main", {
      content: [{ type: "text", text: "same" }],
      event_id: 49,
    })

    expect(chat.messagesByTab.main).toHaveLength(3)
  })

  it("send() does not register a 2s content-signature that would drop a later distinct-id echo", async () => {
    const chat = seedTab()
    const wsSend = vi.fn()
    chat._ws = { readyState: WebSocket.OPEN, send: wsSend }

    await chat.send([{ type: "text", text: "ping" }])
    const frame = JSON.parse(wsSend.mock.calls[0][0])
    expect(frame.event_id).toMatch(/^c_/)
    expect(chat.messagesByTab.main).toHaveLength(1)

    chat._handleUserInput("main", {
      content: [{ type: "text", text: "ping" }],
      pending_id: "c_second",
      event_id: 50,
    })

    expect(chat.messagesByTab.main).toHaveLength(2)
    expect(chat.messagesByTab.main.map((m) => m.eventId)).toEqual([frame.event_id, "c_second"])
  })
})

describe("multimodal identity — admission and replay boundaries", () => {
  it.each(["injection", "processing_start"])(
    "reconciles queued echo before %s without an extra bubble",
    async (next) => {
      const chat = seedTab({ processingByTab: { main: true } })
      chat._ws = { readyState: WebSocket.OPEN, send: vi.fn() }
      await chat.send([IMAGE_A])
      const queued = chat.queuedMessagesByTab.main[0]
      const echo = { content: [IMAGE_A], pending_id: queued.eventId }
      chat._handleUserInput("main", echo)
      expect(chat.messagesByTab.main).toHaveLength(0)
      expect(chat.queuedMessagesByTab.main).toHaveLength(1)
      if (next === "injection") chat._handleUserInputInjected("main", echo)
      else chat._promoteQueuedMessages("main")
      expect(chat.queuedMessagesByTab.main).toHaveLength(0)
      expect(chat.messagesByTab.main).toHaveLength(1)
      expect(chat.messagesByTab.main[0].eventId).toBe(queued.eventId)
      chat._handleUserInputInjected("main", echo)
      expect(chat.messagesByTab.main).toHaveLength(1)
    },
  )

  it("updates the raw queued draft before promotion after a pending edit", async () => {
    const chat = seedTab({ processingByTab: { main: true } })
    chat._ws = { readyState: WebSocket.OPEN, send: vi.fn() }
    await chat.send([IMAGE_A])
    const id = chat.queuedMessagesByTab.main[0].eventId
    chat._handleUserInput("main", { content: [IMAGE_A], pending_id: id })
    chat.editQueuedMessage("main", id, [IMAGE_B])
    chat._promoteQueuedMessages("main")
    expect(chat.messagesByTab.main[0].rawContent).toEqual([IMAGE_B])
    expect(chat.messagesByTab.main[0].contentParts).toEqual([IMAGE_B])
  })

  it("never consumes replayed historical identity for a new legacy input", () => {
    const chat = seedTab()
    const events = seedBranchHistory("same").eventsByTab.main.map((event) => ({
      ...event,
      pending_id: "old-id",
    }))
    chat.messagesByTab.main = _replayEvents([], events).messages
    chat._handleUserInput("main", { content: "same", event_id: 60 })
    expect(chat.messagesByTab.main.filter((row) => row.role === "user")).toHaveLength(2)
  })

  it("retains the complete raw block list through replay and server echoes", () => {
    const content = [
      IMAGE_A,
      { type: "vendor_part", value: "kept" },
      { type: "text", text: "edit" },
    ]
    const chat = seedBranchHistory(content)
    expect(chat.messagesByTab.main[0].rawContent).toEqual(content)
    chat._handleUserInput("main", { content, pending_id: "new-id" })
    expect(chat.messagesByTab.main.at(-1).rawContent).toEqual(content)
  })

  it("legacy fallback checks full media payload, including extension fields", () => {
    const chat = seedTab()
    chat.messagesByTab.main = [
      {
        id: "u_optimistic",
        role: "user",
        _inputEchoPending: true,
        content: "",
        contentParts: [{ ...IMAGE_A, meta: { source_name: "original.png" } }],
      },
    ]
    chat._handleUserInput("main", { content: [{ ...IMAGE_A, meta: { source_name: "other.png" } }] })
    expect(chat.messagesByTab.main).toHaveLength(2)
  })
})

describe("multimodal identity — user_input_injected queue", () => {
  it("promotes the queued row whose eventId equals pending_id even when content matches a later slot", () => {
    const chat = seedTab()
    chat.queuedMessagesByTab.main = [
      {
        id: "q1",
        role: "user",
        content: "same",
        contentParts: [{ type: "text", text: "same" }],
        eventId: "c_first",
        timestamp: "t1",
      },
      {
        id: "q2",
        role: "user",
        content: "same",
        contentParts: [{ type: "text", text: "same" }],
        eventId: "c_second",
        timestamp: "t2",
      },
    ]

    chat._handleUserInputInjected("main", {
      content: [{ type: "text", text: "same" }],
      pending_id: "c_second",
    })

    expect(chat.queuedMessagesByTab.main.map((m) => m.eventId)).toEqual(["c_first"])
    expect(chat.messagesByTab.main).toHaveLength(1)
    expect(chat.messagesByTab.main[0]).toMatchObject({
      id: "q2",
      eventId: "c_second",
      injectedMidTurn: true,
    })
  })

  it("does not pop a same-text queued file when pending_id points at a missing identity", () => {
    const chat = seedTab()
    chat.queuedMessagesByTab.main = [
      {
        id: "q1",
        role: "user",
        content: "caption",
        contentParts: [{ type: "text", text: "caption" }, IMAGE_A],
        eventId: "c_file_a",
        timestamp: "t1",
      },
    ]

    chat._handleUserInputInjected("main", {
      content: [{ type: "text", text: "caption" }, IMAGE_B],
      pending_id: "c_file_missing",
    })

    expect(chat.queuedMessagesByTab.main).toHaveLength(1)
    expect(chat.queuedMessagesByTab.main[0].eventId).toBe("c_file_a")
    expect(chat.messagesByTab.main).toHaveLength(1)
    expect(chat.messagesByTab.main[0].eventId).toBe("c_file_missing")
    expect(chat.messagesByTab.main[0].contentParts).toEqual([
      { type: "text", text: "caption" },
      IMAGE_B,
    ])
  })

  it("legacy identity-poor inject may pop only a unique complete payload candidate", () => {
    const chat = seedTab()
    chat.queuedMessagesByTab.main = [
      {
        id: "q1",
        role: "user",
        content: "unique",
        contentParts: [{ type: "text", text: "unique" }],
        eventId: "c_only",
        timestamp: "t1",
      },
    ]

    chat._handleUserInputInjected("main", {
      content: [{ type: "text", text: "unique" }],
    })

    expect(chat.queuedMessagesByTab.main).toHaveLength(0)
    expect(chat.messagesByTab.main[0].id).toBe("q1")
  })

  it("legacy inject does not pop when two queued rows share the same payload", () => {
    const chat = seedTab()
    chat.queuedMessagesByTab.main = [
      {
        id: "q1",
        role: "user",
        content: "same",
        contentParts: [{ type: "text", text: "same" }],
        eventId: "c_a",
        timestamp: "t1",
      },
      {
        id: "q2",
        role: "user",
        content: "same",
        contentParts: [{ type: "text", text: "same" }],
        eventId: "c_b",
        timestamp: "t2",
      },
    ]

    chat._handleUserInputInjected("main", {
      content: [{ type: "text", text: "same" }],
    })

    expect(chat.queuedMessagesByTab.main).toHaveLength(2)
    expect(chat.messagesByTab.main).toHaveLength(1)
    expect(chat.messagesByTab.main[0].id).not.toBe("q1")
    expect(chat.messagesByTab.main[0].id).not.toBe("q2")
  })

  it("does not match a queued image-only row by empty plaintext fallback", () => {
    const chat = seedTab()
    chat.queuedMessagesByTab.main = [
      {
        id: "q1",
        role: "user",
        content: "",
        contentParts: [IMAGE_A],
        eventId: "c_img_a",
        timestamp: "t1",
      },
    ]

    chat._handleUserInputInjected("main", {
      content: [IMAGE_B],
      pending_id: "c_img_b",
    })

    expect(chat.queuedMessagesByTab.main[0].eventId).toBe("c_img_a")
    expect(chat.messagesByTab.main[0].eventId).toBe("c_img_b")
    expect(chat.messagesByTab.main[0].contentParts).toEqual([IMAGE_B])
  })
})

describe("multimodal identity — edit/regenerate request_id association", () => {
  it("editMessage stamps requestId onto the optimistic edited row", async () => {
    const chat = seedBranchHistory("hello")
    vi.spyOn(agentAPI, "editMessage").mockResolvedValue({
      status: "completed",
      turn_index: 1,
      branch_id: 2,
    })
    vi.spyOn(terrariumAPI, "getHistory").mockResolvedValue({
      events: chat.eventsByTab.main,
      is_processing: false,
    })

    const userIdx = chat.messagesByTab.main.findIndex((m) => m.role === "user")
    const pending = chat.editMessage(userIdx, [{ type: "text", text: "hello" }, IMAGE_A], {
      turnIndex: 1,
      latestBranch: 1,
      userPosition: 0,
      tabId: "main",
    })
    const requestId = chat.branchOperationByTab.main.requestId
    expect(requestId).toBeTruthy()
    const edited = chat.messagesByTab.main.find((m) => m.role === "user")
    expect(edited.eventId).toBe(requestId)
    expect(edited.contentParts).toEqual([{ type: "text", text: "hello" }, IMAGE_A])

    chat._handleUserInput("main", {
      content: [{ type: "text", text: "hello" }, IMAGE_A],
      request_id: requestId,
      event_id: 80,
      turn_index: 1,
      branch_id: 2,
    })
    expect(chat.messagesByTab.main.filter((m) => m.role === "user")).toHaveLength(1)
    expect(chat.messagesByTab.main.find((m) => m.role === "user").contentParts).toEqual([
      { type: "text", text: "hello" },
      IMAGE_A,
    ])

    await pending
    chat._clearBranchResyncTimers()
  })

  it("regenerate keeps the raw ordered original in optimistic history", async () => {
    const raw = [IMAGE_A, { type: "vendor_part", value: "keep" }, { type: "text", text: "caption" }]
    const chat = seedBranchHistory(raw)
    vi.spyOn(agentAPI, "regenerate").mockResolvedValue({
      status: "completed",
      turn_index: 1,
      branch_id: 2,
    })
    vi.spyOn(chat, "_resyncHistory").mockResolvedValue(false)
    await chat.regenerateLastResponse()
    expect(
      chat.eventsByTab.main.find((event) => event._optimistic && event.type === "user_message")
        .content,
    ).toEqual(raw)
  })

  it("regenerateLastResponse stamps requestId onto the optimistic user event", async () => {
    const chat = seedBranchHistory("hello")
    vi.spyOn(agentAPI, "regenerate").mockResolvedValue({
      status: "completed",
      turn_index: 1,
      branch_id: 2,
    })
    vi.spyOn(terrariumAPI, "getHistory").mockResolvedValue({
      events: chat.eventsByTab.main,
      is_processing: false,
    })

    const pending = chat.regenerateLastResponse({ turnIndex: 1, tabId: "main" })
    const requestId = chat.branchOperationByTab.main.requestId
    expect(requestId).toBeTruthy()
    const optimistic = (chat.eventsByTab.main || []).find(
      (evt) => evt._optimistic && evt.type === "user_input",
    )
    expect(optimistic.request_id).toBe(requestId)

    chat._handleUserInput("main", {
      content: "hello",
      request_id: requestId,
      event_id: 81,
      turn_index: 1,
      branch_id: 2,
    })
    expect(chat.messagesByTab.main.filter((m) => m.role === "user")).toHaveLength(1)

    await pending
    chat._clearBranchResyncTimers()
  })
})

describe("accepted mutation failure — execution_error", () => {
  function mockAcceptedFailure(kind, executionError = "provider exploded") {
    const response = {
      status: "completed",
      turn_index: 1,
      branch_id: 2,
      execution_error: executionError,
    }
    if (kind === "edit") vi.spyOn(agentAPI, "editMessage").mockResolvedValue(response)
    else vi.spyOn(agentAPI, "regenerate").mockResolvedValue(response)
    return response
  }

  it.each(["edit", "regenerate"])(
    "%s: completed+execution_error keeps the new branch, resyncs, clears busy, surfaces error, returns ok:true",
    async (kind) => {
      const chat = seedBranchHistory("hello")
      mockAcceptedFailure(kind, "llm refused the turn")
      const canonical = [
        ...chat.eventsByTab.main,
        {
          type: "user_input",
          event_id: 11,
          content: kind === "edit" ? "edited" : "hello",
          turn_index: 1,
          branch_id: 2,
        },
        {
          type: "user_message",
          event_id: 12,
          content: kind === "edit" ? "edited" : "hello",
          turn_index: 1,
          branch_id: 2,
        },
        { type: "processing_start", event_id: 13, turn_index: 1, branch_id: 2 },
        { type: "processing_end", event_id: 14, turn_index: 1, branch_id: 2 },
      ]
      vi.spyOn(terrariumAPI, "getHistory").mockResolvedValue({
        events: canonical,
        is_processing: false,
      })

      const result =
        kind === "edit"
          ? await chat.editMessage(0, "edited", {
              turnIndex: 1,
              latestBranch: 1,
              userPosition: 0,
              tabId: "main",
            })
          : await chat.regenerateLastResponse({ turnIndex: 1, tabId: "main" })

      expect(result).toMatchObject({
        ok: true,
        error: "llm refused the turn",
      })
      expect(chat.branchViewByTab.main[1]).toBe(2)
      expect(chat.processingByTab.main).toBe(false)
      expect(chat.branchOperationByTab.main).toBeFalsy()
      expect(chat.branchOperationErrorByTab.main).toBe("llm refused the turn")
      expect(JSON.stringify(chat.messagesByTab.main)).not.toContain("hello-answer")
    },
  )

  it("pre-admission HTTP error still rolls the optimistic edit back", async () => {
    const chat = seedBranchHistory("hello")
    const previousEvents = JSON.parse(JSON.stringify(chat.eventsByTab.main))
    vi.spyOn(agentAPI, "editMessage").mockRejectedValue(
      Object.assign(Error("Unprocessable"), { response: { status: 422 }, mayHaveRun: false }),
    )

    const result = await chat.editMessage(0, "edited", {
      turnIndex: 1,
      latestBranch: 1,
      userPosition: 0,
      tabId: "main",
    })

    expect(result.ok).toBe(false)
    expect(chat.eventsByTab.main).toEqual(previousEvents)
    expect(chat.processingByTab.main).toBe(false)
    expect(chat.branchOperationErrorByTab.main).toBeTruthy()
  })

  it("lost transport keeps the optimistic branch (existing safeguard)", async () => {
    const chat = seedBranchHistory("hello")
    vi.spyOn(agentAPI, "editMessage").mockRejectedValue(
      Object.assign(Error("timeout of 30000ms exceeded"), {
        code: "ECONNABORTED",
        mayHaveRun: true,
      }),
    )
    vi.spyOn(terrariumAPI, "getHistory").mockResolvedValue({
      events: chat.eventsByTab.main,
      is_processing: true,
    })

    const result = await chat.editMessage(0, "edited", {
      turnIndex: 1,
      latestBranch: 1,
      userPosition: 0,
      tabId: "main",
    })

    expect(result.ok).toBe(true)
    expect(chat.branchViewByTab.main[1]).toBe(2)
    expect(chat._branchResyncPendingByTab.main?.active).toBe(true)
    chat._clearBranchResyncTimers()
  })
})
