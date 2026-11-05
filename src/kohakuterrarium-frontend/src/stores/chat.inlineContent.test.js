import { createPinia, setActivePinia } from "pinia"
import { toRaw } from "vue"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { _convertHistory, _replayEvents, useChatStore } from "./chat.js"

function content() {
  return [
    { type: "text", text: "caption", extra: { values: [null, false, 0, ""] } },
    {
      type: "image_url",
      image_url: { url: `data:image/png;base64,${"A".repeat(1024 * 1024)}`, detail: "low" },
      meta: { nested: [{ name: "original" }] },
    },
    { type: "file", file: { name: "a.txt", content: "payload" }, extra: [[{ keep: true }]] },
    { type: "vendor", extension: { nested: [{ value: "kept" }] } },
    null,
  ]
}

beforeEach(() => setActivePinia(createPinia()))
afterEach(() => vi.restoreAllMocks())

describe("inline content ownership", () => {
  it("normalizes snapshots without serializing inline strings", () => {
    const source = content()
    const stringify = vi.spyOn(JSON, "stringify")
    const parse = vi.spyOn(JSON, "parse")
    const [row] = _convertHistory([{ role: "user", content: source }])
    expect(row.rawContent).toEqual(source)
    expect(row.contentParts.map((part) => part.type)).toEqual(["text", "image_url", "file"])
    expect(stringify).not.toHaveBeenCalled()
    expect(parse).not.toHaveBeenCalled()
  })

  it.each(["snapshot", "replay"])(
    "detaches every %s container but shares raw/display parts",
    (path) => {
      const source = content()
      const rows =
        path === "snapshot"
          ? _convertHistory([{ role: "user", content: source }])
          : _replayEvents(
              [],
              [{ type: "user_message", content: source, event_id: 1, turn_index: 1, branch_id: 0 }],
            ).messages
      const row = rows[0]
      expect(row.rawContent).not.toBe(source)
      expect(row.rawContent[1]).not.toBe(source[1])
      expect(row.contentParts[1]).toBe(row.rawContent[1])
      source[1].meta.nested[0].name = "source changed"
      source[3].extension.nested.push({ value: "new" })
      source.reverse()
      expect(row.rawContent[1].meta.nested[0].name).toBe("original")
      expect(row.rawContent[3].extension.nested).toEqual([{ value: "kept" }])
      row.contentParts[0].extra.values.push("display changed")
      row.rawContent[2].extra[0][0].keep = false
      expect(source.at(-1).extra.values).toEqual([null, false, 0, ""])
      expect(source[2].extra[0][0].keep).toBe(true)
    },
  )

  it("retains authoritative echo identity without serializing or aliasing the incoming payload", () => {
    const chat = useChatStore()
    chat.tabs = ["main"]
    chat.activeTab = "main"
    chat.messagesByTab.main = [
      { id: "optimistic", role: "user", eventId: "pending", content: "old" },
    ]
    const row = chat.messagesByTab.main[0]
    const incoming = content()
    const echo = { content: incoming, pending_id: "pending", event_id: 2 }
    const stringify = vi.spyOn(JSON, "stringify")
    const parse = vi.spyOn(JSON, "parse")
    chat._handleUserInput("main", echo)
    chat._handleUserInput("main", echo)
    expect(chat.messagesByTab.main).toHaveLength(1)
    expect(chat.messagesByTab.main[0]).toBe(row)
    expect(toRaw(row.contentParts[1])).toBe(toRaw(row.rawContent[1]))
    expect(stringify).not.toHaveBeenCalled()
    expect(parse).not.toHaveBeenCalled()
    incoming[1].meta.nested[0].name = "external edit"
    row.rawContent[3].extension.nested[0].value = "row edit"
    expect(row.rawContent[1].meta.nested[0].name).toBe("original")
    expect(incoming[3].extension.nested[0].value).toBe("kept")
    chat._cleanup()
  })
})
