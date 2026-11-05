import { describe, expect, it } from "vitest"

import { _replayEvents } from "@/stores/chat"

describe("trigger events keep the trigger's ui_name", () => {
  it.each([
    [
      {
        type: "activity",
        activity_type: "trigger_fired",
        trigger_id: "trigger_3f",
        ui_name: "qq_feed",
        content: "hi",
      },
    ],
    [{ type: "trigger_fired", trigger_id: "trigger_3f", ui_name: "qq_feed", content: "hi" }],
  ])("records triggerName and triggerUiName from %o", (event) => {
    const { messages } = _replayEvents([], [event])
    const trigger = messages.find((m) => m.role === "trigger")
    expect(trigger).toMatchObject({ triggerName: "trigger_3f", triggerUiName: "qq_feed" })
  })
})
