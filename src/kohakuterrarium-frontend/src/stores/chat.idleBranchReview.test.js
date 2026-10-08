import { createPinia, setActivePinia } from "pinia"
import { beforeEach, describe, expect, it } from "vitest"

import { useChatStore } from "./chat.js"

beforeEach(() => {
  setActivePinia(createPinia())
})

describe("idle branch view cache", () => {
  it("does not override an explicit browsed branch when session info refreshes", () => {
    const chat = useChatStore()
    chat.activeTab = "alice"
    chat.tabs = ["alice"]
    chat.branchViewByTab = { alice: { 1: 1 } }
    chat._browsedBranchByTab = { alice: true }
    chat._handleActivity("alice", {
      activity_type: "session_info",
      branch_view: { 1: 2 },
    })
    expect(chat.branchViewByTab.alice).toEqual({ 1: 1 })
  })

  it("does not retain incompatible old descendants while initializing an authoritative path", () => {
    const chat = useChatStore()
    chat.activeTab = "alice"
    chat.tabs = ["alice"]
    chat.branchViewByTab = { alice: { 1: 1, 2: 1 } }
    chat._adoptAuthoritativeBranchView("alice", { 1: 2 })
    expect(chat.branchViewByTab.alice).toEqual({ 1: 2 })
  })

  it("clears browsed markers when unbinding so a later same-name tab can restore", () => {
    const chat = useChatStore()
    chat.activeTab = "alice"
    chat.tabs = ["alice"]
    chat.branchViewByTab = { alice: { 1: 1 } }
    chat._browsedBranchByTab = { alice: true }
    chat.unbindFromInstance()
    expect(chat._browsedBranchByTab).toEqual({})
    chat.activeTab = "alice"
    chat.tabs = ["alice"]
    chat._adoptAuthoritativeBranchView("alice", { 1: 2 })
    expect(chat.branchViewByTab.alice).toEqual({ 1: 2 })
  })
})
