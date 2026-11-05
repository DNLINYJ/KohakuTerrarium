import { flushPromises, mount } from "@vue/test-utils"
import { afterEach, beforeEach, expect, it, vi } from "vitest"
import { effectScope, reactive, ref } from "vue"

import SideConversationHost from "./SideConversationHost.vue"
import SideConversationModal from "./SideConversationModal.vue"
import { useSideConversation } from "@/composables/useSideConversation"
import SubagentConversationPanel from "./SubagentConversationPanel.vue"
import { terrariumAPI } from "@/utils/api"

const compact = ref(false)
vi.mock("@/composables/useDensity", () => ({ useDensity: () => ({ isCompact: compact }) }))
vi.mock("@/utils/i18n", () => ({ useI18n: () => ({ t: (key) => key }) }))
const wrappers = []
const thread = {
  key: "session:root:job",
  sessionId: "session",
  parent: "root",
  job_id: "job",
  name: "fork",
  run: 0,
}
const transcript = {
  can_receive: true,
  meta: { audience: "user", inherited_message_count: 2, source: { turn_index: 4, branch_id: 2 } },
  messages: [
    { role: "system", content: "parent system" },
    { role: "user", content: "parent private question" },
    { role: "user", content: "[Private context fork instructions]\nprivate policy" },
    { role: "user", content: "side question" },
    { role: "assistant", content: "side answer" },
  ],
}
function show() {
  const wrapper = mount(SideConversationModal, {
    attachTo: document.body,
    props: { thread },
    global: {
      stubs: {
        Teleport: true,
        MarkdownRenderer: { props: ["content"], template: "<div>{{ content }}</div>" },
      },
    },
  })
  wrappers.push(wrapper)
  return wrapper
}
beforeEach(() => {
  compact.value = false
  vi.spyOn(terrariumAPI, "getSubagentConversation").mockResolvedValue(transcript)
  vi.spyOn(terrariumAPI, "executeCreatureCommand").mockResolvedValue({
    data: { type: "side_conversation", job_id: "job" },
  })
})
afterEach(() => {
  wrappers.splice(0).forEach((w) => w.unmount())
  vi.restoreAllMocks()
})

it.each([false, true])(
  "renders shared transcript with inherited context hidden on phone=%s; closes without stopping",
  async (phone) => {
    compact.value = phone
    const wrapper = show()
    await flushPromises()
    expect(terrariumAPI.getSubagentConversation).toHaveBeenCalledWith("session", "root", {
      jobId: "job",
      name: "fork",
    })
    expect(wrapper.text()).toContain("side answer")
    expect(wrapper.text()).toContain("From root · captured context")
    expect(wrapper.text()).toContain("Captured turn 4 · branch 2")
    expect(wrapper.text()).not.toContain("parent private question")
    expect(wrapper.text()).not.toContain("private policy")
    expect(wrapper.text()).toContain("/btw --open job")
    await wrapper.get('[data-test="side-context"]').trigger("click")
    expect(wrapper.text()).toContain("parent private question")
    expect(wrapper.text()).not.toContain("private policy")
    if (phone) await wrapper.get('[data-test="phone-sheet-close"]').trigger("click")
    else await wrapper.findAll("button")[0].trigger("click")
    expect(wrapper.emitted("close")).toHaveLength(1)
    expect(terrariumAPI.executeCreatureCommand).not.toHaveBeenCalled()
  },
)

it.each([false, true])("traps focus and restores the composer on phone=%s", async (phone) => {
  compact.value = phone
  const composer = document.createElement("textarea")
  document.body.append(composer)
  composer.focus()
  const wrapper = show()
  await flushPromises()
  const buttons = wrapper.findAll("button:not(:disabled), textarea:not(:disabled), a[href]")
  const first = buttons[0].element
  const last = buttons.at(-1).element
  expect(document.activeElement).toBe(first)
  first.dispatchEvent(
    new KeyboardEvent("keydown", { key: "Tab", shiftKey: true, bubbles: true, cancelable: true }),
  )
  expect(document.activeElement).toBe(last)
  last.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true }))
  expect(document.activeElement).toBe(first)
  const globalEscape = vi.fn()
  document.addEventListener("kt:modal:escape", globalEscape)
  first.dispatchEvent(
    new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true }),
  )
  expect(wrapper.emitted("close")).toHaveLength(1)
  expect(globalEscape).not.toHaveBeenCalled()
  document.removeEventListener("kt:modal:escape", globalEscape)
  wrapper.unmount()
  expect(document.activeElement).toBe(composer)
  composer.remove()
})

it("continues a completed thread via command, preserves failed draft, and explicitly stops", async () => {
  const legacy = vi.spyOn(terrariumAPI, "sendSubagentMessage")
  const wrapper = show()
  await flushPromises()
  const input = wrapper.get("textarea")
  await input.setValue("follow up")
  terrariumAPI.executeCreatureCommand.mockResolvedValueOnce({ error: "busy" })
  await input.trigger("keydown", { key: "Enter" })
  await flushPromises()
  expect(wrapper.text()).toContain("busy")
  expect(input.element.value).toBe("follow up")
  await input.trigger("keydown", { key: "Enter" })
  await flushPromises()
  expect(terrariumAPI.executeCreatureCommand).toHaveBeenLastCalledWith(
    "session",
    "root",
    "btw",
    "--continue job follow up",
  )
  expect(input.element.value).toBe("")
  expect(legacy).not.toHaveBeenCalled()
  await wrapper.get('[data-test="side-stop"]').trigger("click")
  await flushPromises()
  expect(terrariumAPI.executeCreatureCommand).toHaveBeenLastCalledWith(
    "session",
    "root",
    "btw",
    "--stop job",
  )
})

it("does not expose a parent prefix when metadata is absent", async () => {
  terrariumAPI.getSubagentConversation.mockResolvedValue({ ...transcript, meta: {} })
  const wrapper = show()
  await flushPromises()
  expect(wrapper.text()).not.toContain("parent private question")
  expect(wrapper.text()).not.toContain("side answer")
})

it("ignores a late continuation response after switching the panel target", async () => {
  let finish
  terrariumAPI.executeCreatureCommand.mockImplementation(
    () => new Promise((resolve) => (finish = resolve)),
  )
  const wrapper = show()
  await flushPromises()
  const panel = wrapper.getComponent(SubagentConversationPanel)
  await panel.get("textarea").setValue("first")
  await panel.get("textarea").trigger("keydown", { key: "Enter" })
  await wrapper.setProps({ thread: { ...thread, key: "new", job_id: "new" } })
  await flushPromises()
  await wrapper.get("textarea").setValue("new draft")
  const reads = terrariumAPI.getSubagentConversation.mock.calls.length
  finish({})
  await flushPromises()
  expect(wrapper.get("textarea").element.value).toBe("new draft")
  expect(terrariumAPI.getSubagentConversation).toHaveBeenCalledTimes(reads)
})

it.each([false, true])(
  "updates the host chip and heading from live metadata on phone=%s without leaking targets",
  async (phone) => {
    compact.value = phone
    vi.useFakeTimers()
    const scope = effectScope()
    const chat = reactive({ _instanceId: "session", _instanceGeneration: 1 })
    const tabKey = ref("root")
    const side = scope.run(() => useSideConversation({ chat, tabKey }))
    const open = (job, title) =>
      side.open(
        { type: "side_conversation", job_id: job, title, status: "running" },
        { sessionId: "session", creatureId: "root" },
      )
    const reads = []
    terrariumAPI.getSubagentConversation.mockImplementation(
      () => new Promise((resolve) => reads.push(resolve)),
    )
    open("agent_fork_first")
    const wrapper = mount(SideConversationHost, {
      props: { side },
      global: {
        stubs: {
          Teleport: true,
          MarkdownRenderer: { props: ["content"], template: "<div>{{ content }}</div>" },
        },
      },
    })
    wrappers.push(wrapper)
    try {
      expect(wrapper.get('button[title="/btw --open agent_fork_first"]').text()).toBe("By the way")
      expect(wrapper.get('[role="dialog"]').attributes("aria-label")).toBe("By the way")
      expect(wrapper.find('[data-test="side-processing"]').exists()).toBe(true)
      reads.shift()({
        ...transcript,
        meta: { ...transcript.meta, status: "done", title: "Heuristic title" },
      })
      await flushPromises()
      expect(wrapper.get('button[title="/btw --open agent_fork_first"]').text()).toBe(
        "Heuristic title",
      )
      expect(wrapper.get('[role="dialog"]').attributes("aria-label")).toBe("Heuristic title")
      expect(wrapper.find('[data-test="side-processing"]').exists()).toBe(false)
      await vi.advanceTimersByTimeAsync(1500)
      reads.shift()({
        ...transcript,
        meta: { ...transcript.meta, status: "done", title: "Refined title" },
      })
      await flushPromises()
      expect(wrapper.get('button[title="/btw --open agent_fork_first"]').text()).toBe(
        "Refined title",
      )
      expect(wrapper.get('[role="dialog"]').attributes("aria-label")).toBe("Refined title")
      expect(wrapper.get("code").text()).toBe("/btw --open agent_fork_first")
      await vi.advanceTimersByTimeAsync(1500)
      const stale = reads.shift()
      open("agent_fork_second", "Second title")
      await flushPromises()
      reads.shift()({
        ...transcript,
        meta: { ...transcript.meta, status: "done", title: "Second refined" },
      })
      await flushPromises()
      stale({
        ...transcript,
        meta: { ...transcript.meta, status: "running", title: "Stale title" },
      })
      await flushPromises()
      expect(wrapper.get('[role="dialog"]').attributes("aria-label")).toBe("Second refined")
      expect(wrapper.get('button[title="/btw --open agent_fork_first"]').text()).toBe(
        "Refined title",
      )
      expect(wrapper.get('button[title="/btw --open agent_fork_second"]').text()).toBe(
        "Second refined",
      )
      expect(wrapper.text()).not.toContain("Stale title")
    } finally {
      wrapper.unmount()
      scope.stop()
      vi.useRealTimers()
    }
  },
)
