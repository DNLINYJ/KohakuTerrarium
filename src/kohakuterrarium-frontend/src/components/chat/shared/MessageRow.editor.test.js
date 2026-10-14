// Real-row UI for order-preserving inline edits: replace by original index,
// append new attachments, and disable merged multi-text editing.
import { mount } from "@vue/test-utils"
import { createPinia, setActivePinia } from "pinia"
import { beforeEach, describe, expect, it, vi } from "vitest"

import ChatMessage from "../ChatMessage.vue"
import { useChatStore } from "@/stores/chat"

function file(name, type, content) {
  return new File([content], name, { type })
}

function mountUser(pinia, content) {
  const store = useChatStore()
  store._instanceId = "instance"
  const message = {
    id: "m1",
    role: "user",
    content,
    contentParts: content,
    turnIndex: 1,
    branchId: 1,
    latestBranch: 1,
    userPosition: 0,
    locator: { eventId: 1, turnIndex: 1, branchId: 1 },
  }
  store.messagesByTab.main = [message]
  store.activeTab = "main"
  const wrapper = mount(ChatMessage, {
    props: { message, messageIdx: 0, tabId: "main" },
    global: {
      plugins: [pinia],
      stubs: {
        MarkdownRenderer: true,
        ToolCallBlock: true,
        ToolBatchGroup: true,
        UIEventBlock: true,
        ContentParts: true,
      },
    },
  })
  return { wrapper, store, message }
}

describe("MessageRow order-preserving inline editor", () => {
  let pinia

  beforeEach(() => {
    Object.defineProperty(window, "localStorage", {
      configurable: true,
      value: { getItem: vi.fn(() => null), setItem: vi.fn(), removeItem: vi.fn() },
    })
    pinia = createPinia()
    setActivePinia(pinia)
  })

  it("offers replace on the current attachment and appends a new file at the end", async () => {
    const original = [
      {
        type: "image_url",
        image_url: { url: "file:///tmp/a.png" },
        meta: { source_name: "dup.png" },
      },
      { type: "text", text: "caption" },
      { type: "file", file: { name: "dup.png", path: "/workspace/dup.png" } },
    ]
    const { wrapper, store } = mountUser(pinia, original)
    const editSpy = vi.spyOn(store, "editMessage").mockResolvedValue({ ok: true })
    await wrapper.get('[aria-label="Edit and rerun message"]').trigger("click")

    const chips = wrapper.findAll(".message-edit-attachment")
    expect(chips).toHaveLength(2)
    expect(wrapper.get('[aria-label="Replace dup.png"]').exists()).toBe(true)
    expect(wrapper.findAll('[aria-label^="Replace "]')).toHaveLength(2)

    const replaceInput = wrapper.get('input[data-edit-target="0"]')
    Object.defineProperty(replaceInput.element, "files", {
      value: [file("replaced.png", "image/png", "NEW")],
    })
    await replaceInput.trigger("change")

    const addInput = wrapper.get('input[data-edit-role="add-file"]')
    Object.defineProperty(addInput.element, "files", {
      value: [file("extra.txt", "text/plain", "TAIL")],
    })
    await addInput.trigger("change")
    await wrapper.get('[aria-label="Save and rerun"]').trigger("click")

    await vi.waitFor(() => expect(editSpy).toHaveBeenCalledTimes(1))
    const payload = editSpy.mock.calls[0][1]
    expect(payload.map((part) => part.type)).toEqual(["image_url", "text", "file", "file"])
    expect(payload[0].meta.source_name).toBe("replaced.png")
    expect(payload[2].file.path).toBe("/workspace/dup.png")
    expect(payload[3].file.content).toBe("TAIL")
    expect(original[0].meta.source_name).toBe("dup.png")
  })

  it("keeps a merged multi-text textarea read-only with a visible explanation", async () => {
    const original = [
      { type: "text", text: "one" },
      { type: "file", file: { name: "a.txt", path: "/a.txt" } },
      { type: "text", text: "two" },
    ]
    const { wrapper, store } = mountUser(pinia, original)
    const editSpy = vi.spyOn(store, "editMessage").mockResolvedValue({ ok: true })
    await wrapper.get('[aria-label="Edit and rerun message"]').trigger("click")

    const textarea = wrapper.get("textarea.message-edit-inline")
    expect(textarea.attributes("readonly")).toBeDefined()
    expect(wrapper.get('[data-testid="edit-text-restricted"]').text()).toMatch(/merged text/i)
    expect(textarea.element.value).toBe("one\ntwo")
    await wrapper.get('[aria-label="Remove a.txt"]').trigger("click")
    await wrapper.get('[aria-label="Save and rerun"]').trigger("click")
    await vi.waitFor(() => expect(editSpy).toHaveBeenCalledTimes(1))
    expect(editSpy.mock.calls[0][1]).toEqual([
      { type: "text", text: "one" },
      { type: "text", text: "two" },
    ])
    expect(original).toEqual([
      { type: "text", text: "one" },
      { type: "file", file: { name: "a.txt", path: "/a.txt" } },
      { type: "text", text: "two" },
    ])
  })

  it("shows a visible error instead of saving a read-only multi-text deletion that rebinds [[file:1]]", async () => {
    const original = [
      { type: "text", text: "keep" },
      { type: "file", file: { name: "dup.txt", path: "/workspace/a.txt", content: "FILE-A" } },
      { type: "text", text: "see [[file:1]]" },
      { type: "file", file: { name: "dup.txt", path: "/workspace/b.txt", content: "FILE-B" } },
      { type: "file", file: { name: "dup.txt", path: "/workspace/c.txt", content: "FILE-C" } },
    ]
    const { wrapper, store } = mountUser(pinia, original)
    const editSpy = vi.spyOn(store, "editMessage").mockResolvedValue({ ok: true })
    await wrapper.get('[aria-label="Edit and rerun message"]').trigger("click")
    expect(wrapper.get("textarea.message-edit-inline").attributes("readonly")).toBeDefined()
    await wrapper.get('[aria-label="Remove dup.txt"]').trigger("click")
    await wrapper.get('[aria-label="Save and rerun"]').trigger("click")
    await vi.waitFor(() =>
      expect(wrapper.get('[role="alert"]').text()).toMatch(/numeric file reference|placeholder/i),
    )
    expect(editSpy).not.toHaveBeenCalled()
    expect(original[2].text).toBe("see [[file:1]]")
    expect(original[1].file.content).toBe("FILE-A")
  })

  it("saves replacing the referenced same-name file without rewriting readonly text", async () => {
    const original = [
      { type: "text", text: "keep" },
      { type: "file", file: { name: "dup.txt", path: "/workspace/a.txt", content: "FILE-A" } },
      { type: "text", text: "see [[file:1]]" },
      { type: "file", file: { name: "dup.txt", path: "/workspace/b.txt", content: "FILE-B" } },
      { type: "file", file: { name: "dup.txt", path: "/workspace/c.txt", content: "FILE-C" } },
    ]
    const { wrapper, store } = mountUser(pinia, original)
    const editSpy = vi.spyOn(store, "editMessage").mockResolvedValue({ ok: true })
    await wrapper.get('[aria-label="Edit and rerun message"]').trigger("click")
    const replaceInput = wrapper.get('input[data-edit-target="1"]')
    Object.defineProperty(replaceInput.element, "files", {
      value: [file("dup.txt", "text/plain", "FILE-B2")],
    })
    await replaceInput.trigger("change")
    await wrapper.get('[aria-label="Save and rerun"]').trigger("click")
    await vi.waitFor(() => expect(editSpy).toHaveBeenCalledTimes(1))
    const payload = editSpy.mock.calls[0][1]
    expect(payload.map((part) => part.type)).toEqual(["text", "file", "text", "file", "file"])
    expect(payload[2].text).toBe("see [[file:1]]")
    expect(payload[3].file.content).toBe("FILE-B2")
    expect(original[3].file.content).toBe("FILE-B")
  })
})
