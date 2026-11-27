import { mount } from "@vue/test-utils"
import { createPinia, setActivePinia } from "pinia"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import RailItem from "@/components/shell/RailItem.vue"
import RailContextMenu from "@/components/shell/RailContextMenu.vue"
import TabContextMenu from "@/components/shell/TabContextMenu.vue"
import { useTabsStore } from "@/stores/tabs"

const stubs = { ActionButtons: { template: "<div />" } }

beforeEach(() => {
  setActivePinia(createPinia())
  localStorage.clear()
})

afterEach(() => {
  delete window.__TAURI_INTERNALS__
})

function tauri() {
  const invoke = vi.fn(async () => true)
  window.__TAURI_INTERNALS__ = { invoke }
  return invoke
}

describe("tab context menu", () => {
  const sessionTab = { id: "attach:graph-1", kind: "attach", target: "graph-1" }

  it("moves a session tab into a window of its own and closes the tab", async () => {
    const invoke = tauri()
    const tabs = useTabsStore()
    tabs.openTab(sessionTab)
    const wrapper = mount(TabContextMenu, {
      props: { tab: sessionTab, index: 0, total: 1 },
      global: { stubs },
    })

    await wrapper.get('[data-test="tab-move-to-window"]').trigger("click")
    await vi.waitFor(() => expect(tabs.byId[sessionTab.id]).toBeUndefined())
    expect(invoke).toHaveBeenCalledWith("open_popout_window", expect.anything())
    expect(wrapper.emitted("close")).toBeTruthy()
  })

  it("offers nothing for tabs that are not sessions or without a shell that can", () => {
    tauri()
    const settings = mount(TabContextMenu, {
      props: { tab: { id: "settings", kind: "settings" }, index: 0, total: 1 },
      global: { stubs },
    })
    expect(settings.find('[data-test="tab-move-to-window"]').exists()).toBe(false)

    delete window.__TAURI_INTERNALS__
    const browser = mount(TabContextMenu, {
      props: { tab: sessionTab, index: 0, total: 1 },
      global: { stubs },
    })
    expect(browser.find('[data-test="tab-move-to-window"]').exists()).toBe(false)
  })
})

describe("rail context menu", () => {
  const instance = { id: "i1", graph_id: "graph-1", config_name: "alpha" }

  it("opens at the clicked row and dismisses without activating the row", async () => {
    const wrapper = mount(RailItem, {
      props: { instance: { ...instance, is_live: true, runtime_id: "graph-1" } },
      global: { stubs },
    })
    const tabs = useTabsStore()
    const open = vi.spyOn(tabs, "openSurface")
    const activate = vi.spyOn(tabs, "activateTab")
    try {
      await wrapper.trigger("contextmenu", { clientX: 43, clientY: 611 })
      const menu = wrapper.getComponent(RailContextMenu)
      expect(menu.props("position")).toEqual({ x: 43, y: 611 })
      expect(document.body.contains(menu.element)).toBe(true)
      await menu.trigger("click")
      expect(wrapper.findComponent(RailContextMenu).exists()).toBe(false)
      expect(open).not.toHaveBeenCalled()
      expect(activate).not.toHaveBeenCalled()
    } finally {
      wrapper.unmount()
    }
  })

  it("opens a session in a window of its own and keeps it open here", async () => {
    const invoke = tauri()
    const wrapper = mount(RailContextMenu, {
      props: { instance, position: { x: 40, y: 80 } },
      global: { stubs },
    })

    await wrapper.get('[data-test="rail-open-in-window"]').trigger("click")
    expect(invoke).toHaveBeenCalledWith("open_popout_window", {
      args: [expect.objectContaining({ scope: "graph-1", title: "alpha" })],
    })
    expect(wrapper.emitted("close")).toBeTruthy()
  })

  it("offers nothing in a browser", () => {
    const wrapper = mount(RailContextMenu, {
      props: { instance, position: { x: 40, y: 80 } },
      global: { stubs },
    })
    expect(wrapper.find('[data-test="rail-open-in-window"]').exists()).toBe(false)
  })
})
