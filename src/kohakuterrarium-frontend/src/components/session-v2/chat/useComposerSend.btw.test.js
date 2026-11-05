import { flushPromises, mount } from "@vue/test-utils"
import { createPinia, setActivePinia } from "pinia"
import { computed, defineComponent, h, ref } from "vue"
import { afterEach, expect, it, vi } from "vitest"

import { useChatStore } from "@/stores/chat"
import { terrariumAPI } from "@/utils/api"
import { useComposerSend } from "./useComposerSend"

let wrapper
afterEach(() => {
  wrapper?.unmount()
  vi.restoreAllMocks()
})

it("sends while busy, isolates delayed results and discards responses from an old session generation", async () => {
  setActivePinia(createPinia())
  const chat = useChatStore("btw-v2")
  chat._instanceId = "session"
  chat._instanceGraphId = "graph"
  chat.activeTab = "root"
  chat.tabs = ["root", "other"]
  chat.messagesByTab = { root: [], other: [] }
  chat.processingByTab.root = true
  let resolve
  const command = vi.spyOn(terrariumAPI, "executeCreatureCommand").mockImplementation(
    () =>
      new Promise((done) => {
        resolve = done
      }),
  )
  const onSent = vi.fn()
  let composer
  wrapper = mount(
    defineComponent({
      setup() {
        composer = useComposerSend({
          chat,
          tabKey: computed(() => chat.activeTab),
          instance: ref({ id: "session" }),
          onSent,
        })
        return () => h("div")
      },
    }),
  )
  composer.inputText.value = "/btw question"
  expect(composer.processing.value).toBe(false)
  const pending = composer.send()
  await flushPromises()
  expect(command).toHaveBeenCalledWith("graph", "root", "btw", "question")
  expect(chat.processingByTab.root).toBe(true)
  expect(chat.messagesByTab.root).toEqual([])
  expect(onSent).not.toHaveBeenCalled()
  chat.activeTab = "other"
  resolve({ data: { type: "side_conversation", job_id: "job", name: "fork" } })
  await pending
  expect(composer.sideConversation.active.value).toBeNull()
  chat.activeTab = "root"
  expect(composer.sideConversation.active.value.job_id).toBe("job")
  await flushPromises()
  composer.inputText.value = "/btw second"
  const stale = composer.send()
  await flushPromises()
  chat._instanceGeneration++
  resolve({ data: { type: "side_conversation", job_id: "stale" } })
  await stale
  expect(composer.sideConversation.active.value).toBeNull()
  expect(composer.sideConversation.remembered.value).toEqual([])
})
