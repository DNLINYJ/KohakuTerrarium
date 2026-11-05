import { mount } from "@vue/test-utils"
import { createPinia, setActivePinia } from "pinia"
import { afterEach, beforeEach, describe, expect, it } from "vitest"

import ToolCallBlock from "@/components/chat/ToolCallBlock.vue"
import ConversationMessage from "@/components/chat/shared/ConversationMessage"
import {
  registerToolRenderer,
  registerTriggerRenderer,
  toolRenderers,
  triggerRenderers,
  unregisterRenderers,
} from "@/ext/renderers"

const MarkdownStub = {
  props: ["content", "origin"],
  template: "<div class='md'>{{ content }}</div>",
}

describe("in-chat renderers", () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    registerToolRenderer("qq_send", {
      key: "ext:p/r",
      component: {
        props: ["call"],
        template: "<div class='ext-render'>{{ call.args.text }}</div>",
      },
    })
    registerTriggerRenderer("qq_feed", {
      key: "ext:p/t",
      component: {
        props: ["event"],
        template: "<div class='ext-trigger'>{{ event.triggerContent }}</div>",
      },
    })
  })
  afterEach(() => unregisterRenderers(["ext:p/r", "ext:p/t"]))

  it("replaces only the owning tool's block body", () => {
    const tc = {
      type: "tool",
      id: "t1",
      name: "qq_send",
      kind: "tool",
      status: "done",
      args: { text: "hi" },
      result: "raw out",
    }
    const opts = { global: { stubs: { MarkdownRenderer: MarkdownStub } } }
    const custom = mount(ToolCallBlock, { props: { tc, expanded: true }, ...opts })
    expect(custom.find(".ext-render").text()).toBe("hi")
    expect(custom.text()).not.toContain("raw out")
    const plain = mount(ToolCallBlock, {
      props: { tc: { ...tc, name: "read" }, expanded: true },
      ...opts,
    })
    expect(plain.find(".ext-render").exists()).toBe(false)
    expect(plain.text()).toContain("raw out")
  })

  it("replaces only the owning trigger's event body", () => {
    const message = {
      id: "m1",
      role: "trigger",
      content: "qq",
      triggerContent: "new message",
      triggerName: "qq_feed",
    }
    expect(mount(ConversationMessage, { props: { message } }).find(".ext-trigger").text()).toBe(
      "new message",
    )
    const plain = mount(ConversationMessage, {
      props: { message: { ...message, triggerName: "timer" } },
    })
    expect(plain.find(".ext-trigger").exists()).toBe(false)
    expect(plain.text()).toContain("new message")
  })

  it("matches a runtime-added trigger by its ui_name", () => {
    const message = {
      id: "m2",
      role: "trigger",
      content: "qq",
      triggerContent: "via ui_name",
      triggerName: "trigger_3fa9c1",
      triggerUiName: "qq_feed",
    }
    expect(mount(ConversationMessage, { props: { message } }).find(".ext-trigger").text()).toBe(
      "via ui_name",
    )
  })

  it("unregisters by contribution key", () => {
    unregisterRenderers(["ext:p/r"])
    expect(toolRenderers.has("qq_send")).toBe(false)
    expect(triggerRenderers.has("qq_feed")).toBe(true)
  })
})
