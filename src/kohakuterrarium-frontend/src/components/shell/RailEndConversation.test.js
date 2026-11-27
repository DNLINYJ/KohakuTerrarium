import { flushPromises, mount } from "@vue/test-utils"
import { ElMessageBox } from "element-plus"
import { createPinia, setActivePinia } from "pinia"
import { afterEach, beforeEach, expect, it, vi } from "vitest"

import RailContextMenu from "@/components/shell/RailContextMenu.vue"
import RailItem from "@/components/shell/RailItem.vue"
import { useConversationsStore } from "@/stores/conversations"
import { confirmNative } from "@/utils/hostBridge"

const instance = { id: "one", config_name: "Sample" }
let wrapper
let end

beforeEach(() => {
  setActivePinia(createPinia())
  end = vi.spyOn(useConversationsStore(), "endConversation").mockResolvedValue()
})

afterEach(() => {
  wrapper?.unmount()
  delete window.pywebview
  delete window.__TAURI_INTERNALS__
  vi.restoreAllMocks()
})

async function endFromRow() {
  wrapper = mount(RailItem, { props: { instance } })
  await wrapper.trigger("contextmenu")
  wrapper.getComponent(RailContextMenu).vm.$emit("end")
  await flushPromises()
}

it.each([true, false])("uses the pywebview decision (%s) directly", async (answer) => {
  const native = vi.fn(async () => answer)
  window.pywebview = { api: { confirm_action: native } }
  const fallback = vi.spyOn(ElMessageBox, "confirm").mockResolvedValue("confirm")
  await endFromRow()
  expect(native).toHaveBeenCalledWith(expect.stringContaining("Sample"), expect.any(String))
  expect(fallback).not.toHaveBeenCalled()
  expect(end).toHaveBeenCalledTimes(answer ? 1 : 0)
})

it.each(["confirm", "cancel", "close"])("handles %s in the browser dialog", async (decision) => {
  const fallback = vi.spyOn(ElMessageBox, "confirm")
  if (decision === "confirm") fallback.mockResolvedValue("confirm")
  else fallback.mockRejectedValue(decision)
  await endFromRow()
  expect(fallback).toHaveBeenCalledWith(
    expect.any(String),
    expect.stringContaining("Sample"),
    expect.objectContaining({ type: "warning" }),
  )
  expect(end).toHaveBeenCalledTimes(decision === "confirm" ? 1 : 0)
})

it.each([
  ["Ok", true],
  ["Cancel", false],
])("uses the Tauri dialog result %s", async (result, accepted) => {
  const invoke = vi.fn().mockResolvedValue(result)
  window.__TAURI_INTERNALS__ = { invoke }
  expect(await confirmNative("End", "Keep history?")).toBe(accepted)
  expect(invoke).toHaveBeenCalledWith("plugin:dialog|message", {
    title: "End",
    message: "Keep history?",
    kind: "warning",
    buttons: "OkCancel",
  })
})

it("allows the page to confirm when an older Tauri host lacks permission", async () => {
  window.__TAURI_INTERNALS__ = { invoke: vi.fn().mockRejectedValue(new Error("unavailable")) }
  expect(await confirmNative("End", "Keep history?")).toBeUndefined()
})
