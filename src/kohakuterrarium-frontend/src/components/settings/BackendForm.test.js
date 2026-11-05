import { flushPromises, mount } from "@vue/test-utils"
import { beforeEach, describe, expect, it, vi } from "vitest"
import ElementPlus from "element-plus"
vi.mock("@/utils/api", () => ({ settingsAPI: { getBackendTypes: vi.fn() } }))
vi.mock("@/utils/i18n", () => ({ useI18n: () => ({ t: (key) => key }) }))
import { settingsAPI } from "@/utils/api"
import BackendForm from "./BackendForm.vue"
const form = (backend_type) => ({
  name: "mine",
  backend_type,
  base_url: "",
  provider_name: "",
  provider_native_tools: [],
})
function mountForm(backendType = "openai") {
  return mount(BackendForm, {
    props: { form: form(backendType) },
    global: { plugins: [ElementPlus] },
  })
}
function optionValues(wrapper) {
  return wrapper.findAllComponents({ name: "ElOption" }).map((option) => option.props("value"))
}
describe("backend type choices", () => {
  beforeEach(() => vi.clearAllMocks())
  it("adds package backend types after the built-in ones", async () => {
    settingsAPI.getBackendTypes.mockResolvedValue({
      backend_types: [
        { name: "openai", source: "builtin", credentials: "api_key" },
        {
          name: "echo",
          display_name: "Echo",
          source: "package",
          package: "pkg-x",
          credentials: "external",
        },
      ],
    })
    const wrapper = mountForm()
    await flushPromises()
    expect(optionValues(wrapper)).toEqual(["openai", "codex", "anthropic", "echo"])
  })
  it("keeps the built-in choices when the type list cannot load", async () => {
    settingsAPI.getBackendTypes.mockRejectedValue(new Error("offline"))
    const wrapper = mountForm()
    await flushPromises()
    expect(optionValues(wrapper)).toEqual(["openai", "codex", "anthropic"])
  })
  it("hides the endpoint field only for a type that manages its own login", async () => {
    settingsAPI.getBackendTypes.mockResolvedValue({
      backend_types: [
        { name: "echo", source: "package", package: "p", credentials: "external" },
        { name: "keyed", source: "package", package: "p", credentials: "api_key" },
      ],
    })
    const own = mountForm("echo")
    await flushPromises()
    expect(own.text()).not.toContain("settings.backends.baseUrl")
    const keyed = mountForm("keyed")
    await flushPromises()
    expect(keyed.text()).toContain("settings.backends.baseUrl")
    const builtin = mountForm("openai")
    await flushPromises()
    expect(builtin.text()).toContain("settings.backends.baseUrl")
  })
})
