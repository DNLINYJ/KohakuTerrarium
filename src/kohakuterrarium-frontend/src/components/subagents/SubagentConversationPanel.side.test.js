import { flushPromises, mount } from "@vue/test-utils"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import SubagentConversationPanel from "./SubagentConversationPanel.vue"
import { terrariumAPI } from "@/utils/api"

vi.mock("@/utils/i18n", () => ({ useI18n: () => ({ t: (key) => key }) }))
const wrappers = []
const indicator = '[data-test="side-processing"]'
const transcript = (status, title = "Side title") => ({
  can_receive: true,
  meta: { status, title, audience: "user", inherited_message_count: 0 },
  messages: [{ role: "assistant", content: "side answer" }],
})
function deferred() {
  let resolve
  const promise = new Promise((done) => (resolve = done))
  return { promise, resolve }
}
function show(overrides = {}) {
  const wrapper = mount(SubagentConversationPanel, {
    props: {
      sessionId: "session",
      parent: "root",
      jobId: "job",
      name: "fork",
      live: true,
      sideConversation: true,
      status: "running",
      ...overrides,
    },
    global: {
      stubs: { MarkdownRenderer: { props: ["content"], template: "<div>{{ content }}</div>" } },
    },
  })
  wrappers.push(wrapper)
  return wrapper
}
beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(terrariumAPI, "getSubagentConversation").mockResolvedValue(transcript("running"))
  vi.spyOn(terrariumAPI, "executeCreatureCommand").mockResolvedValue({})
})
afterEach(() => {
  wrappers.splice(0).forEach((wrapper) => wrapper.unmount())
  vi.useRealTimers()
  vi.restoreAllMocks()
})

it.each(["done", "error", "cancelled"])(
  "shows queued processing immediately, then obeys live %s despite messageability",
  async (status) => {
    const initial = deferred()
    terrariumAPI.getSubagentConversation.mockReturnValueOnce(initial.promise)
    const wrapper = show()
    expect(wrapper.get(indicator).text()).toBe("chat.processing")
    expect(wrapper.get(indicator).attributes("role")).toBe("status")
    expect(wrapper.find(".kt-transcript-processing__dot").exists()).toBe(true)
    initial.resolve(transcript("running"))
    await flushPromises()
    expect(wrapper.find(indicator).exists()).toBe(true)
    terrariumAPI.getSubagentConversation.mockResolvedValue(transcript(status))
    await vi.advanceTimersByTimeAsync(1500)
    await flushPromises()
    expect(wrapper.find(indicator).exists()).toBe(false)
    expect(wrapper.find("textarea").exists()).toBe(true)
  },
)

it("starts a followup before its command/poll finishes and ignores a pre-command terminal poll", async () => {
  terrariumAPI.getSubagentConversation.mockResolvedValue(transcript("done"))
  const wrapper = show()
  await flushPromises()
  expect(wrapper.find(indicator).exists()).toBe(false)
  const stale = deferred()
  terrariumAPI.getSubagentConversation.mockReturnValueOnce(stale.promise)
  await vi.advanceTimersByTimeAsync(1500)
  const command = deferred()
  terrariumAPI.executeCreatureCommand.mockReturnValueOnce(command.promise)
  await wrapper.get("textarea").setValue("follow up")
  await wrapper.get("textarea").trigger("keydown", { key: "Enter" })
  expect(wrapper.find(indicator).exists()).toBe(true)
  stale.resolve(transcript("done"))
  await flushPromises()
  expect(wrapper.find(indicator).exists()).toBe(true)
  const refresh = deferred()
  terrariumAPI.getSubagentConversation.mockReturnValueOnce(refresh.promise)
  command.resolve({})
  await flushPromises()
  expect(wrapper.find(indicator).exists()).toBe(true)
  refresh.resolve(transcript("running"))
  await flushPromises()
  expect(wrapper.find(indicator).exists()).toBe(true)
  await vi.advanceTimersByTimeAsync(1500)
  await flushPromises()
  expect(wrapper.find(indicator).exists()).toBe(false)
})

it("clears optimistic processing on command error and preserves the draft", async () => {
  terrariumAPI.getSubagentConversation.mockResolvedValue(transcript("done"))
  const wrapper = show()
  await flushPromises()
  const command = deferred()
  terrariumAPI.executeCreatureCommand.mockReturnValueOnce(command.promise)
  await wrapper.get("textarea").setValue("retry me")
  await wrapper.get("textarea").trigger("keydown", { key: "Enter" })
  expect(wrapper.find(indicator).exists()).toBe(true)
  command.resolve({ error: "Cannot continue" })
  await flushPromises()
  expect(wrapper.find(indicator).exists()).toBe(false)
  expect(wrapper.get("textarea").element.value).toBe("retry me")
  expect(wrapper.get('[role="alert"]').text()).toBe("Cannot continue")
})

it("does not resurrect optimistic processing while transcript refreshes keep failing", async () => {
  terrariumAPI.getSubagentConversation.mockResolvedValue(transcript("done"))
  const wrapper = show()
  await flushPromises()
  terrariumAPI.getSubagentConversation.mockRejectedValue(new Error("offline"))
  await wrapper.get("textarea").setValue("follow up")
  await wrapper.get("textarea").trigger("keydown", { key: "Enter" })
  await flushPromises()
  expect(wrapper.find(indicator).exists()).toBe(false)
  expect(wrapper.text()).toContain("chat.subagent.unavailable")
  await vi.advanceTimersByTimeAsync(1500)
  await flushPromises()
  expect(wrapper.find(indicator).exists()).toBe(false)
  expect(wrapper.text()).toContain("chat.subagent.unavailable")
  terrariumAPI.getSubagentConversation.mockResolvedValue(transcript("error"))
  await vi.advanceTimersByTimeAsync(1500)
  await flushPromises()
  expect(wrapper.find(indicator).exists()).toBe(false)
  expect(wrapper.text()).not.toContain("chat.subagent.unavailable")
  expect(wrapper.find("textarea").exists()).toBe(true)
})

it("stops the indicator on explicit cancellation before the refresh completes", async () => {
  const wrapper = show()
  await flushPromises()
  const stale = deferred()
  terrariumAPI.getSubagentConversation.mockReturnValueOnce(stale.promise)
  await vi.advanceTimersByTimeAsync(1500)
  const refresh = deferred()
  terrariumAPI.getSubagentConversation.mockReturnValueOnce(refresh.promise)
  await wrapper.get('[data-test="side-stop"]').trigger("click")
  await flushPromises()
  expect(wrapper.find(indicator).exists()).toBe(false)
  stale.resolve(transcript("running"))
  await flushPromises()
  expect(wrapper.find(indicator).exists()).toBe(false)
  refresh.resolve(transcript("cancelled"))
  await flushPromises()
  expect(wrapper.find(indicator).exists()).toBe(false)
})

it("emits polled titles only for the current target and not after disposal", async () => {
  const onMetadata = vi.fn()
  const wrapper = show({ onMetadata })
  await flushPromises()
  expect(wrapper.emitted("metadata").at(-1)[0]).toMatchObject({
    sessionId: "session",
    parent: "root",
    jobId: "job",
    meta: { title: "Side title" },
  })
  const stale = deferred()
  terrariumAPI.getSubagentConversation.mockReturnValueOnce(stale.promise)
  await vi.advanceTimersByTimeAsync(1500)
  terrariumAPI.getSubagentConversation.mockResolvedValue(transcript("done", "New title"))
  await wrapper.setProps({ jobId: "new-job" })
  await flushPromises()
  const events = wrapper.emitted("metadata").length
  stale.resolve(transcript("running", "Stale title"))
  await flushPromises()
  expect(wrapper.emitted("metadata")).toHaveLength(events)
  expect(wrapper.emitted("metadata").at(-1)[0]).toMatchObject({
    jobId: "new-job",
    meta: { title: "New title" },
  })
  expect(wrapper.find(indicator).exists()).toBe(false)
  const disposed = deferred()
  terrariumAPI.getSubagentConversation.mockReturnValueOnce(disposed.promise)
  await vi.advanceTimersByTimeAsync(1500)
  wrapper.unmount()
  disposed.resolve(transcript("running", "Disposed title"))
  await flushPromises()
  expect(onMetadata).toHaveBeenCalledTimes(events)
})
