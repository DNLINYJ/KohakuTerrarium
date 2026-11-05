import { mount } from "@vue/test-utils"
import { describe, expect, it, vi } from "vitest"
import ElementPlus from "element-plus"
vi.mock("@/utils/i18n", () => ({
  useI18n: () => ({ t: (key, args) => (args ? `${key}:${args.name}` : key) }),
}))
import PackageProviderRow from "./PackageProviderRow.vue"
function row(backend) {
  return mount(PackageProviderRow, {
    props: { backend: { name: "echo", backend_type: "echo", package: "pkg-x", ...backend } },
    global: { plugins: [ElementPlus] },
  })
}
describe("package provider row", () => {
  it("shows the owning package and an own-login status without any action", () => {
    const wrapper = row({ credentials: "external" })
    expect(wrapper.text()).toContain("settings.providers.fromPackage:pkg-x")
    expect(wrapper.text()).toContain("settings.providers.packageManaged")
    expect(wrapper.findAll("button")).toHaveLength(0)
  })
  it("shows the API-key status for a key-based package backend", () => {
    const wrapper = row({ credentials: "api_key", available: false })
    expect(wrapper.text()).toContain("settings.providers.packageKey")
    expect(wrapper.findComponent({ name: "ElTag" }).props("type")).toBe("info")
  })
})
