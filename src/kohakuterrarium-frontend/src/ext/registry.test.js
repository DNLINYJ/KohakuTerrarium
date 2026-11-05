import { createPinia, setActivePinia } from "pinia"
import { beforeEach, describe, expect, it, vi } from "vitest"

import {
  extensionTabLaunchers,
  extensionTabMeta,
  openExtensionTab,
  provideContributionComponent,
  registerPackageContributions,
  registeredContributionKeys,
  unregisterAllContributions,
} from "@/ext/registry"
import { toolRenderers, triggerRenderers } from "@/ext/renderers"
import { contributionComponents, extState, parseContributionKey } from "@/ext/state"
import { useLayoutStore } from "@/stores/layout"
import { settingsSections, visibleSettingsSections } from "@/stores/settingsSectionRegistry"
import { railGroups, tabKinds } from "@/stores/tabKindRegistry"
import { useTabsStore } from "@/stores/tabs"
import { parseTabId } from "@/utils/tabsUrl"

const C = { template: "<div />" }

function record(name, contributes) {
  return { package: name, enabled: true, status: "idle", contributes }
}

describe("extension registry", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    unregisterAllContributions()
  })

  it("maps every mount kind onto its dashboard registry and removes them again", () => {
    const rec = record("alpha", [
      { kind: "tab", id: "t", label: "Tab", icon: "i-x", owner: "package:alpha" },
      { kind: "panel", id: "p", label: "Panel", owner: "plugin:counter" },
      { kind: "rail", id: "r", label: "Rail", order: 5, owner: "package:alpha" },
      { kind: "settings", id: "s", label: "Sect", order: 15, owner: "package:alpha" },
      { kind: "tool-renderer", id: "tr", label: "TR", owner: "tool:send" },
      { kind: "trigger-renderer", id: "gr", label: "GR", owner: "trigger:feed" },
    ])
    extState.packages = [rec]
    registerPackageContributions(rec)
    expect(tabKinds.has("ext:alpha/t")).toBe(true)
    expect(useLayoutStore().getPanel("ext:alpha/p").label).toBe("Panel")
    expect(railGroups.get("ext:alpha/r").order).toBe(5)
    expect(settingsSections.get("ext:alpha/s").label).toBe("Sect")
    expect(toolRenderers.get("send").key).toBe("ext:alpha/tr")
    expect(triggerRenderers.get("feed").key).toBe("ext:alpha/gr")
    expect(registeredContributionKeys()).toHaveLength(6)

    expect(extensionTabMeta("ext:alpha/t")).toEqual({ label: "Tab", icon: "i-x" })
    expect(extensionTabMeta("settings")).toBeNull()
    expect(extensionTabLaunchers()).toEqual([
      { key: "ext:alpha/t", label: "Tab", icon: "i-x", package: "alpha" },
    ])
    openExtensionTab("alpha/t")
    expect(useTabsStore().byId["ext:alpha/t"]).toMatchObject({ kind: "ext:alpha/t", label: "Tab" })
    expect(parseTabId("ext:alpha/t")).toEqual({ kind: "ext:alpha/t", id: "ext:alpha/t" })
    expect(parseContributionKey("ext:@builtin/budget")).toEqual({ pkg: "@builtin", id: "budget" })

    unregisterAllContributions()
    expect(tabKinds.has("ext:alpha/t")).toBe(false)
    expect(useLayoutStore().getPanel("ext:alpha/p")).toBeNull()
    expect(railGroups.has("ext:alpha/r") || settingsSections.has("ext:alpha/s")).toBe(false)
    expect(toolRenderers.size + triggerRenderers.size).toBe(0)
  })

  it("keeps the first renderer for a name and orders settings sections", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {})
    const a = record("a", [{ kind: "tool-renderer", id: "r", label: "r", owner: "tool:send" }])
    const b = record("b", [
      { kind: "tool-renderer", id: "r", label: "r", owner: "tool:send" },
      { kind: "settings", id: "early", label: "E", order: 15, owner: "package:b" },
    ])
    extState.packages = [a, b]
    registerPackageContributions(a)
    registerPackageContributions(b)
    expect(toolRenderers.get("send").key).toBe("ext:a/r")
    expect(warn).toHaveBeenCalled()
    expect(registeredContributionKeys()).toEqual(["ext:a/r", "ext:b/early"])
    expect(visibleSettingsSections().map((s) => s.id)).toEqual(["ext:b/early"])
    warn.mockRestore()
  })

  it("validates components a package registers", () => {
    const rec = record("alpha", [{ kind: "settings", id: "s", label: "S", owner: "package:alpha" }])
    extState.packages = [rec]
    registerPackageContributions(rec)
    expect(() => provideContributionComponent("alpha", "panel", "s", C)).toThrow(
      /declared as a settings/,
    )
    expect(() => provideContributionComponent("alpha", "settings", "s", null)).toThrow(
      /needs a component/,
    )
    // An undeclared runtime-owned mount needs an explicit owner of the right kind.
    expect(() => provideContributionComponent("alpha", "panel", "dyn", C)).toThrow(/options.owner/)
    expect(() =>
      provideContributionComponent("alpha", "panel", "dyn", C, { owner: "tool:x" }),
    ).toThrow(/options.owner/)
    expect(() =>
      provideContributionComponent("alpha", "settings", "dyn", C, { owner: "package:other" }),
    ).toThrow(/options.owner/)
    provideContributionComponent("alpha", "panel", "dyn", C, { owner: "plugin:x", label: "Dyn" })
    provideContributionComponent("alpha", "settings", "auto", C)
    expect(useLayoutStore().getPanel("ext:alpha/dyn").label).toBe("Dyn")
    expect(rec.contributes.find((c) => c.id === "auto").owner).toBe("package:alpha")
    expect(contributionComponents.has("ext:alpha/dyn")).toBe(true)
  })
})
