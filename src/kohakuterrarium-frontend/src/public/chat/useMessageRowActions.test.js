// Lifecycle guards for the shared message-row actions: a pending async copy or
// edit must only settle onto the SAME live view. The stale results must never
// dispatch a mutation, toast, or reopen an editor on the new owner's row.
//
// The file-prepare delay is a REAL I/O seam: ``buildMessageParts`` awaits the
// attachment's ``File.text()``, so the test drives a genuinely async conversion
// instead of mocking the attachment module. No source-text or size regexes.
import { mount } from "@vue/test-utils"
import { defineComponent, h, nextTick, reactive, ref } from "vue"
import { afterEach, describe, expect, it, vi } from "vitest"

const { errorSpy } = vi.hoisted(() => ({ errorSpy: vi.fn() }))
vi.mock("element-plus", () => ({ ElMessage: { error: errorSpy } }))

import { useMessageRowActions } from "./useMessageRowActions"
import { provideMessageEditDrafts } from "../../components/chat/shared/messageEditDrafts"

function deferred() {
  let resolve
  let reject
  const promise = new Promise((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

function userMessage(overrides = {}) {
  return {
    id: "m1",
    role: "user",
    content: "original",
    locator: { eventId: 1, turnIndex: 1, branchId: 1 },
    ...overrides,
  }
}

function fileAttachment(text, name = "note.txt") {
  return {
    id: `a_${name}`,
    name,
    kind: "file",
    file: { name, type: "text/plain", size: 4, text: () => text.promise },
  }
}

// Mounts a real component so the composable runs with a live effect scope and
// the ``messageActions`` inject seam; exposes the returned action handle plus
// the reactive inputs the test needs to mutate mid-flight.
const mounted = []
afterEach(() => {
  for (const wrapper of mounted.splice(0)) wrapper.unmount()
  errorSpy.mockClear()
})

function mountActions({
  message = userMessage(),
  messagesByTab,
  editMessage,
  writeClipboard,
  getViewOwner,
} = {}) {
  const api = {}
  const Harness = defineComponent({
    name: "MessageRowActionsHarness",
    setup() {
      const props = reactive({ message, messageIdx: 0 })
      const messageTab = ref("main")
      const chat = reactive({
        activeTab: "main",
        tabs: ["main"],
        _instanceId: "inst",
        _instanceGraphId: "inst",
        _instanceGeneration: 0,
        branchOperationByTab: {},
        branchOperationErrorByTab: {},
        messagesByTab: messagesByTab || { main: [message] },
        editMessage: editMessage || vi.fn(async () => ({ ok: true })),
      })
      Object.assign(
        api,
        useMessageRowActions({
          props,
          chat,
          messageTab,
          writeClipboard: writeClipboard || (() => Promise.resolve()),
          t: (key) => key,
          getViewOwner,
        }),
      )
      api.__props = props
      api.__chat = chat
      api.__messageTab = messageTab
      return () => h("div")
    },
  })
  const show = ref(true)
  const Provider = defineComponent({
    setup() {
      provideMessageEditDrafts()
      return () => (show.value ? h(Harness) : null)
    },
  })
  const wrapper = mount(Provider)
  mounted.push(wrapper)
  return { wrapper, api, show }
}

describe("useMessageRowActions clipboard ownership", () => {
  it.each(["instance", "message", "ABA"])(
    "ignores a late clipboard failure after %s identity changed without a host probe",
    async (change) => {
      const gate = deferred()
      const { api } = mountActions({ writeClipboard: () => gate.promise })
      const pending = api.copyMessage()
      if (change === "instance") api.__chat._instanceId = "other"
      if (change === "message") api.__props.message.locator.branchId = 2
      if (change === "ABA") {
        api.__messageTab.value = "other"
        api.__messageTab.value = "main"
      }
      gate.reject(Error("late denied"))
      expect(await pending).toBe(false)
      expect(errorSpy).not.toHaveBeenCalled()
    },
  )

  it("surfaces a copy failure exactly once on the view that requested it", async () => {
    errorSpy.mockClear()
    const writeClipboard = vi.fn(() => Promise.reject(new Error("denied")))
    const { api } = mountActions({ writeClipboard })

    const ok = await api.copyMessage()

    expect(ok).toBe(false)
    expect(writeClipboard).toHaveBeenCalledTimes(1)
    expect(errorSpy).toHaveBeenCalledTimes(1)
  })

  it("does not toast a late copy failure after the view owner changed", async () => {
    errorSpy.mockClear()
    const gate = deferred()
    const ownerRef = ref("owner-A")
    const writeClipboard = vi.fn(() => gate.promise)
    const { api } = mountActions({ writeClipboard, getViewOwner: () => ownerRef.value })

    const pending = api.copyMessage()
    ownerRef.value = "owner-B"
    gate.reject(new Error("late denied"))

    expect(await pending).toBe(false)
    expect(writeClipboard).toHaveBeenCalledTimes(1)
    expect(errorSpy).not.toHaveBeenCalled()
  })

  it("does not toast a copy failure after the row unmounted", async () => {
    errorSpy.mockClear()
    const gate = deferred()
    const writeClipboard = vi.fn(() => gate.promise)
    const { wrapper, api } = mountActions({ writeClipboard })

    const pending = api.copyMessage()
    wrapper.unmount()
    gate.reject(new Error("gone"))

    expect(await pending).toBe(false)
    expect(errorSpy).not.toHaveBeenCalled()
  })
})

describe("useMessageRowActions edit lifecycle", () => {
  it("does not revive a pinned draft when the host owner switches away and back in one tick", async () => {
    const owner = ref("A")
    const gate = deferred()
    const { api } = mountActions({ getViewOwner: () => owner.value })
    api.startEdit()
    api.editText.value = "stale pinned draft"
    api.editAttachments.value.push(fileAttachment(gate))
    const pending = api.confirmEdit()
    owner.value = "B"
    owner.value = "A"
    gate.resolve("old file")
    await pending
    expect(api.__chat.editMessage).not.toHaveBeenCalled()
    expect(api.editText.value).toBe("")
    expect(api.editAttachments.value).toEqual([])
  })

  it.each([
    "in-place locator",
    "branch locator",
    "closed tab",
    "ABA tab",
    "missing target",
    "instance id",
  ])("does not submit a prepared edit after %s replacement", async (change) => {
    const fileText = deferred()
    const editMessage = vi.fn(async () => ({ ok: true }))
    const { api } = mountActions({ editMessage })
    api.startEdit()
    api.editAttachments.value.push(fileAttachment(fileText))
    const pending = api.confirmEdit()
    if (change === "in-place locator") api.__props.message.locator.eventId = 9
    if (change === "branch locator") api.__props.message.locator.branchId = 2
    if (change === "closed tab") api.__chat.tabs = []
    if (change === "ABA tab") {
      api.__chat.tabs = []
      api.__chat.tabs = ["main"]
    }
    if (change === "missing target")
      api.__chat.messagesByTab.main = [
        userMessage({ id: "neighbor", locator: { eventId: 9, turnIndex: 2, branchId: 1 } }),
      ]
    if (change === "instance id") api.__chat._instanceId = "other"
    fileText.resolve("body")
    await pending
    expect(editMessage).not.toHaveBeenCalled()
    expect(api.editSaving.value).toBe(false)
  })

  it("does not apply a late file read error after the host view changed", async () => {
    const owner = ref("A")
    const gate = deferred()
    const { api } = mountActions({ getViewOwner: () => owner.value })
    api.startEdit()
    api.editAttachments.value.push(fileAttachment(gate))
    const pending = api.confirmEdit()
    owner.value = "B"
    gate.reject(Error("old file error"))
    await pending
    expect(api.editError.value).toBe("")
    expect(api.editSaving.value).toBe(false)
  })

  it("allows a clicked nonactive pane to finish preparing its own edit", async () => {
    const gate = deferred()
    const editMessage = vi.fn(async () => ({ ok: true }))
    const { api } = mountActions({ editMessage })
    api.startEdit()
    api.editAttachments.value.push(fileAttachment(gate))
    const pending = api.confirmEdit()
    api.__chat.activeTab = "other"
    gate.resolve("correct pane")
    await pending
    expect(editMessage.mock.calls[0][2].tabId).toBe("main")
    expect(editMessage.mock.calls[0][1][1].file.content).toBe("correct pane")
  })

  it("cancels a prepared edit when the view owner changed during file preparation", async () => {
    const fileText = deferred()
    const editMessage = vi.fn(async () => ({ ok: true }))
    const ownerRef = ref("owner-A")
    const { api } = mountActions({ editMessage, getViewOwner: () => ownerRef.value })

    api.editing.value = true
    api.editText.value = "draft text"
    api.editAttachments.value.push(fileAttachment(fileText))
    const pending = api.confirmEdit()
    await nextTick()

    ownerRef.value = "owner-B"
    fileText.resolve("body")
    await pending

    expect(editMessage).not.toHaveBeenCalled()
    expect(api.editText.value).toBe("")
    expect(api.editing.value).toBe(false)
    expect(api.editSaving.value).toBe(false)
  })

  it("cancels a prepared edit when the instance generation changed during preparation", async () => {
    const fileText = deferred()
    const editMessage = vi.fn(async () => ({ ok: true }))
    const { api } = mountActions({ editMessage })

    api.editing.value = true
    api.editText.value = "draft text"
    api.editAttachments.value.push(fileAttachment(fileText))
    const pending = api.confirmEdit()
    await nextTick()

    api.__chat._instanceGeneration = 1
    fileText.resolve("body")
    await pending

    expect(editMessage).not.toHaveBeenCalled()
    expect(api.editText.value).toBe("")
  })

  it("cancels a prepared edit when the row locator changed under the same id", async () => {
    const fileText = deferred()
    const editMessage = vi.fn(async () => ({ ok: true }))
    const { api } = mountActions({ editMessage })

    api.editing.value = true
    api.editText.value = "draft text"
    api.editAttachments.value.push(fileAttachment(fileText))
    const pending = api.confirmEdit()
    await nextTick()

    // Same id, different conversation locator => a different logical row.
    api.__props.message = userMessage({
      id: "m1",
      content: "other",
      locator: { eventId: 9, turnIndex: 2, branchId: 1 },
    })
    fileText.resolve("body")
    await pending

    expect(editMessage).not.toHaveBeenCalled()
  })

  it("re-anchors the dispatched edit to the live row index after a prepend shifted the projection", async () => {
    const editMessage = vi.fn(async () => ({ ok: true }))
    const message = userMessage({
      id: "m2",
      content: "target",
      locator: { eventId: 5, turnIndex: 2, branchId: 1 },
    })
    const older = userMessage({
      id: "m1",
      content: "older",
      locator: { eventId: 1, turnIndex: 1, branchId: 1 },
    })
    // props.messageIdx is 0, but the live projection puts the target at 1.
    const { api } = mountActions({
      message,
      editMessage,
      messagesByTab: { main: [older, message] },
    })

    api.editing.value = true
    api.editText.value = "draft text"
    await api.confirmEdit()

    expect(editMessage).toHaveBeenCalledTimes(1)
    expect(editMessage.mock.calls[0][0]).toBe(1)
  })

  it("keeps the busy state released and shows no error for a superseded result", async () => {
    const editMessage = vi.fn(async () => ({ superseded: true }))
    const { api } = mountActions({ editMessage })

    api.editing.value = true
    api.editText.value = "draft text"
    await api.confirmEdit()

    expect(editMessage).toHaveBeenCalledTimes(1)
    expect(api.editSaving.value).toBe(false)
    expect(api.editError.value).toBe("")
  })
})

describe("useMessageRowActions order-preserving save", () => {
  const originalParts = [
    {
      type: "image_url",
      image_url: { url: "file:///tmp/before.png" },
      meta: { source_name: "dup.png", vendor: "keep" },
    },
    { type: "text", text: "", extra: { keep: true } },
    {
      type: "file",
      file: { name: "note.txt", path: "/workspace/note.txt", mime: "text/plain" },
    },
    { type: "text", text: "reserved", meta: { vendor: "x" } },
    { type: "text", text: "second" },
    {
      type: "file",
      file: { name: "note.txt", path: "/workspace/other.txt", mime: "text/plain" },
    },
  ]

  function partsMessage(content) {
    return userMessage({ content, contentParts: content })
  }

  it("saves a no-op as the exact original raw block list", async () => {
    const editMessage = vi.fn(async () => ({ ok: true }))
    const message = partsMessage(originalParts)
    const { api } = mountActions({ message, editMessage, messagesByTab: { main: [message] } })
    api.startEdit()
    await api.confirmEdit()
    expect(editMessage).toHaveBeenCalledTimes(1)
    expect(editMessage.mock.calls[0][1]).toEqual(originalParts)
    expect(originalParts[1].text).toBe("")
  })

  it("replaces a single text block in place without regrouping attachments", async () => {
    const source = [
      { type: "file", file: { name: "a.txt", path: "/a.txt" } },
      { type: "text", text: "hello", extra: { keep: true } },
      {
        type: "image_url",
        image_url: { url: "file:///tmp/pic.png" },
        meta: { source_name: "pic.png" },
      },
    ]
    const editMessage = vi.fn(async () => ({ ok: true }))
    const message = partsMessage(source)
    const { api } = mountActions({ message, editMessage, messagesByTab: { main: [message] } })
    api.startEdit()
    api.editText.value = "world"
    await api.confirmEdit()
    expect(editMessage.mock.calls[0][1]).toEqual([
      { type: "file", file: { name: "a.txt", path: "/a.txt" } },
      { type: "text", text: "world", extra: { keep: true } },
      {
        type: "image_url",
        image_url: { url: "file:///tmp/pic.png" },
        meta: { source_name: "pic.png" },
      },
    ])
    expect(source[1].text).toBe("hello")
  })

  it("deletes/replaces by original occurrence and appends new attachments at the end", async () => {
    const editMessage = vi.fn(async () => ({ ok: true }))
    const message = partsMessage(originalParts)
    const { api } = mountActions({ message, editMessage, messagesByTab: { main: [message] } })
    api.startEdit()
    expect(api.editAttachments.value.map((item) => item.originalIndex)).toEqual([0, 2, 5])
    api.removeEditAttachment(1)
    const replacement = new File(["NEW"], "replaced.png", { type: "image/png" })
    Object.defineProperty(replacement, "size", { value: 3 })
    api.replaceEditAttachment(0, replacement, "image")
    const extra = new File(["TAIL"], "extra.txt", { type: "text/plain" })
    Object.defineProperty(extra, "size", { value: 4 })
    api.onEditFileChange({ target: { files: [extra], value: "x" } }, "file")
    await api.confirmEdit()
    const payload = editMessage.mock.calls[0][1]
    expect(payload.map((part) => part.type)).toEqual([
      "image_url",
      "text",
      "text",
      "text",
      "file",
      "file",
    ])
    expect(payload[0].meta.source_name).toBe("replaced.png")
    expect(payload[4].file.path).toBe("/workspace/other.txt")
    expect(payload[5].file.content).toBe("TAIL")
    expect(originalParts[2].file.path).toBe("/workspace/note.txt")
  })

  it("keeps sourceContent and original indices across an optimistic unmount/remount", async () => {
    const gate = deferred()
    const editMessage = vi.fn(() => gate.promise)
    const message = partsMessage(originalParts)
    const { api, show } = mountActions({
      message,
      editMessage,
      messagesByTab: { main: [message] },
    })
    api.startEdit()
    expect(api.editTextRestricted.value).toBe(true)
    const pending = api.confirmEdit()
    await nextTick()
    show.value = false
    await nextTick()
    show.value = true
    await nextTick()
    expect(api.editing.value).toBe(true)
    expect(api.editTextRestricted.value).toBe(true)
    expect(api.editAttachments.value.map((item) => item.originalIndex)).toEqual([0, 2, 5])
    gate.resolve({ ok: false, error: "offline" })
    await pending
    api.editText.value = "should not merge"
    await api.confirmEdit()
    expect(editMessage).toHaveBeenCalledTimes(1)
    expect(api.editError.value).toMatch(/merged text/i)
    expect(originalParts[4].text).toBe("second")
  })

  function filteredParts(content) {
    return content.filter((part) => part && ["text", "image_url", "file"].includes(part.type))
  }

  function rawRow(rawContent) {
    return userMessage({
      content: rawContent
        .filter((part) => part?.type === "text")
        .map((part) => part.text || "")
        .join("\n"),
      contentParts: filteredParts(rawContent),
      rawContent,
    })
  }

  it("rejects editing unknown raw parts instead of silently dropping them", async () => {
    const rawContent = [
      {
        type: "image_url",
        image_url: { url: "file:///tmp/pic.png" },
        meta: { source_name: "pic.png", vendor: "keep" },
      },
      { type: "unknown", payload: { raw: 1 }, meta: { vendor: "x" } },
      { type: "text", text: "hello", extra: { keep: true } },
      { type: "file", file: { name: "note.txt", path: "/workspace/note.txt" } },
    ]
    const editMessage = vi.fn(async () => ({ ok: true }))
    const message = rawRow(rawContent)
    expect(message.contentParts.map((part) => part.type)).toEqual(["image_url", "text", "file"])
    const { api } = mountActions({ message, editMessage, messagesByTab: { main: [message] } })
    api.startEdit()
    expect(api.editing.value).toBe(false)
    expect(api.editError.value).toBe("chat.editUnsupportedParts")
    expect(editMessage).not.toHaveBeenCalled()
    expect(message.rawContent).toEqual(rawContent)
  })

  it("appends an image-only caption and keeps it after deleting the sole image", async () => {
    const rawContent = [
      {
        type: "image_url",
        image_url: { url: "file:///tmp/only.png" },
        meta: { source_name: "only.png" },
      },
    ]
    const editMessage = vi.fn(async () => ({ ok: true }))
    const message = rawRow(rawContent)
    expect(message.contentParts).toEqual(rawContent)
    const { api } = mountActions({ message, editMessage, messagesByTab: { main: [message] } })
    api.startEdit()
    api.editText.value = "a caption"
    await api.confirmEdit()
    expect(editMessage.mock.calls[0][1]).toEqual([
      {
        type: "image_url",
        image_url: { url: "file:///tmp/only.png" },
        meta: { source_name: "only.png" },
      },
      { type: "text", text: "a caption" },
    ])

    const deleted = vi.fn(async () => ({ ok: true }))
    const second = rawRow(rawContent)
    const { api: api2 } = mountActions({
      message: second,
      editMessage: deleted,
      messagesByTab: { main: [second] },
    })
    api2.startEdit()
    api2.removeEditAttachment(0)
    api2.editText.value = "just text"
    await api2.confirmEdit()
    expect(deleted.mock.calls[0][1]).toEqual([{ type: "text", text: "just text" }])
  })

  it("ignores mutate/reorder of the live original after the draft snapshot is opened", async () => {
    const rawContent = [
      {
        type: "image_url",
        image_url: { url: "file:///tmp/pic.png" },
        meta: { source_name: "pic.png", vendor: "keep" },
      },
      { type: "image_url", image_url: { url: "file:///tmp/second.png" }, meta: { vendor: "x" } },
      { type: "text", text: "hello", extra: { keep: true } },
      { type: "file", file: { name: "note.txt", path: "/workspace/note.txt" } },
    ]
    const editMessage = vi.fn(async () => ({ ok: true }))
    const message = rawRow(rawContent)
    const { api } = mountActions({ message, editMessage, messagesByTab: { main: [message] } })
    api.startEdit()
    const snapshot = api.editSourceContent.value
    expect(snapshot).not.toBe(rawContent)
    rawContent.reverse()
    rawContent[0].file.path = "/mutated"
    rawContent[3].meta.vendor = "mutated"
    message.contentParts.reverse()
    api.editText.value = "world"
    await api.confirmEdit()
    expect(editMessage.mock.calls[0][1]).toEqual([
      {
        type: "image_url",
        image_url: { url: "file:///tmp/pic.png" },
        meta: { source_name: "pic.png", vendor: "keep" },
      },
      { type: "image_url", image_url: { url: "file:///tmp/second.png" }, meta: { vendor: "x" } },
      { type: "text", text: "world", extra: { keep: true } },
      { type: "file", file: { name: "note.txt", path: "/workspace/note.txt" } },
    ])
    expect(snapshot[3].file.path).toBe("/workspace/note.txt")
  })

  it("captures a deep snapshot of sourceContent at confirm so later mutation cannot rewrite the save", async () => {
    const rawContent = [
      { type: "text", text: "hello" },
      { type: "file", file: { name: "note.txt", path: "/workspace/note.txt" } },
    ]
    const gate = deferred()
    const editMessage = vi.fn(async () => ({ ok: true }))
    const message = rawRow(rawContent)
    const { api } = mountActions({ message, editMessage, messagesByTab: { main: [message] } })
    api.startEdit()
    const extra = {
      id: "new_async",
      name: "late.txt",
      kind: "file",
      file: { name: "late.txt", type: "text/plain", size: 4, text: () => gate.promise },
    }
    api.editAttachments.value.push(extra)
    const pending = api.confirmEdit()
    api.editSourceContent.value.reverse()
    api.editSourceContent.value[0].file.path = "/mutated-during-await"
    gate.resolve("body")
    await pending
    const payload = editMessage.mock.calls[0][1]
    expect(payload.map((part) => part.type)).toEqual(["text", "file", "file"])
    expect(payload[0].text).toBe("hello")
    expect(payload[1].file.path).toBe("/workspace/note.txt")
    expect(payload[2].file.content).toBe("body")
  })

  it("surfaces a numeric placeholder rebind as a visible edit error and does not dispatch", async () => {
    const source = [
      { type: "text", text: "keep" },
      { type: "file", file: { name: "dup.txt", path: "/workspace/a.txt", content: "FILE-A" } },
      { type: "text", text: "see [[file:1]]" },
      { type: "file", file: { name: "dup.txt", path: "/workspace/b.txt", content: "FILE-B" } },
      { type: "file", file: { name: "dup.txt", path: "/workspace/c.txt", content: "FILE-C" } },
    ]
    const editMessage = vi.fn(async () => ({ ok: true }))
    const message = partsMessage(source)
    const { api } = mountActions({ message, editMessage, messagesByTab: { main: [message] } })
    api.startEdit()
    expect(api.editTextRestricted.value).toBe(true)
    api.removeEditAttachment(0)
    await api.confirmEdit()
    expect(editMessage).not.toHaveBeenCalled()
    expect(api.editing.value).toBe(true)
    expect(api.editError.value).toMatch(/numeric file reference|placeholder/i)
    expect(source[2].text).toBe("see [[file:1]]")
  })
})
