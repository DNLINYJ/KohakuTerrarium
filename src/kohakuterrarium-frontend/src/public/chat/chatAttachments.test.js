import { describe, expect, it } from "vitest"

import {
  buildEditedMessageParts,
  buildMessageParts,
  contentToEditableDraft,
  detectAttachmentKind,
  genericFileToPart,
  imageFileToPart,
  validateAttachment,
  validateAttachments,
} from "@kohakuterrarium/chat-ui"

function file(name, type, content) {
  return new File([content], name, { type })
}

describe("public chat attachment helpers", () => {
  it("converts an image to a named data URL part", async () => {
    const part = await imageFileToPart(file("cat.png", "image/png", "pixels"))
    expect(part).toMatchObject({
      type: "image_url",
      image_url: { detail: "low" },
      meta: { source_type: "attachment", source_name: "cat.png" },
    })
    expect(part.image_url.url).toMatch(/^data:image\/png;base64,/)
  })

  it("converts UTF-8 text and binary files", async () => {
    expect(await genericFileToPart(file("readme.md", "", "héllo"))).toMatchObject({
      file: {
        content: "héllo",
        data_base64: null,
        encoding: "utf-8",
        mime: "application/octet-stream",
      },
    })
    expect(
      await genericFileToPart(file("data.bin", "application/octet-stream", "abc")),
    ).toMatchObject({
      file: { content: null, data_base64: "YWJj", encoding: "base64" },
    })
  })

  it("detects type and validates kind and size limits", () => {
    expect(detectAttachmentKind(file("x.png", "image/png", "x"))).toBe("image")
    expect(detectAttachmentKind(file("x.txt", "text/plain", "x"))).toBe("file")
    expect(
      validateAttachment({ name: "x.png", type: "text/plain", size: 1 }, "image"),
    ).toMatchObject({ code: "not-image" })
    expect(
      validateAttachment({ name: "x.bin", type: "", size: 11 }, "file", { maxAttachmentBytes: 10 }),
    ).toMatchObject({ code: "too-large", limit: 10 })
    expect(validateAttachment({ name: "x.png", type: "image/png", size: 1 }, "image")).toBeNull()
  })

  it("aggregates accepted attachments and error surfaces", () => {
    const result = validateAttachments(
      [
        { name: "ok.txt", type: "text/plain", size: 2 },
        { name: "huge.png", type: "image/png", size: 20 },
      ],
      { maxImageBytes: 10 },
    )
    expect(result.attachments).toEqual([{ file: expect.anything(), name: "ok.txt", kind: "file" }])
    expect(result.errors).toEqual([
      expect.objectContaining({ code: "too-large", name: "huge.png" }),
    ])
  })

  it("builds aggregate parts and preserves editable existing parts", async () => {
    const parts = await buildMessageParts(" hello ", [
      { file: file("a.txt", "text/plain", "A"), name: "a.txt", kind: "file" },
    ])
    expect(parts).toEqual([
      { type: "text", text: " hello " },
      expect.objectContaining({ type: "file", file: expect.objectContaining({ content: "A" }) }),
    ])
    expect(contentToEditableDraft(parts)).toMatchObject({
      text: " hello ",
      attachments: [{ name: "a.txt", kind: "file" }],
    })
  })
})

describe("order-preserving inline editor helpers", () => {
  const original = [
    {
      type: "image_url",
      image_url: { url: "file:///tmp/before.png" },
      meta: { source_name: "dup.png", vendor: "keep" },
    },
    { type: "text", text: "", extra: { keep: true } },
    {
      type: "file",
      file: { name: "note.txt", path: "/workspace/note.txt", mime: "text/plain" },
    },
    { type: "unknown", payload: { raw: 1 }, meta: { vendor: "x" } },
    { type: "text", text: "second" },
    {
      type: "file",
      file: { name: "note.txt", path: "/workspace/other.txt", mime: "text/plain" },
    },
  ]

  it("captures original-block indices and does not regroup history order", () => {
    const draft = contentToEditableDraft(original)
    expect(draft.textRestricted).toBe(true)
    expect(draft.text).toBe("\nsecond")
    expect(draft.sourceContent).toEqual(original)
    expect(draft.sourceContent).not.toBe(original)
    expect(draft.attachments.map((item) => item.originalIndex)).toEqual([0, 2, 5])
    expect(draft.attachments.map((item) => item.name)).toEqual(["dup.png", "note.txt", "note.txt"])
    expect(original[0].meta.vendor).toBe("keep")
  })

  it("no-op editing returns the same raw block list including empty text, unknown, and extras", async () => {
    const draft = contentToEditableDraft(original)
    const edited = await buildEditedMessageParts(original, draft.text, draft.attachments)
    expect(edited).toEqual(original)
    expect(edited).not.toBe(original)
    expect(original).toEqual([
      {
        type: "image_url",
        image_url: { url: "file:///tmp/before.png" },
        meta: { source_name: "dup.png", vendor: "keep" },
      },
      { type: "text", text: "", extra: { keep: true } },
      {
        type: "file",
        file: { name: "note.txt", path: "/workspace/note.txt", mime: "text/plain" },
      },
      { type: "unknown", payload: { raw: 1 }, meta: { vendor: "x" } },
      { type: "text", text: "second" },
      {
        type: "file",
        file: { name: "note.txt", path: "/workspace/other.txt", mime: "text/plain" },
      },
    ])
  })

  it("replaces a single text block at its original location and keeps extras", async () => {
    const source = [
      { type: "file", file: { name: "a.txt", path: "/a.txt" } },
      { type: "text", text: "hello", extra: { keep: true }, cache_control: { type: "ephemeral" } },
      {
        type: "image_url",
        image_url: { url: "file:///tmp/pic.png" },
        meta: { source_name: "pic.png" },
      },
    ]
    const edited = await buildEditedMessageParts(
      source,
      "world",
      contentToEditableDraft(source).attachments,
    )
    expect(edited).toEqual([
      { type: "file", file: { name: "a.txt", path: "/a.txt" } },
      {
        type: "text",
        text: "world",
        extra: { keep: true },
        cache_control: { type: "ephemeral" },
      },
      {
        type: "image_url",
        image_url: { url: "file:///tmp/pic.png" },
        meta: { source_name: "pic.png" },
      },
    ])
    expect(source[1].text).toBe("hello")
  })

  it("deletes and replaces attachments by original block occurrence, then appends new files", async () => {
    const draft = contentToEditableDraft(original)
    const remaining = draft.attachments.filter((item) => item.originalIndex !== 2)
    remaining[0] = {
      ...remaining[0],
      existingPart: undefined,
      file: file("replaced.png", "image/png", "NEW"),
      kind: "image",
      name: "replaced.png",
    }
    remaining.push({
      file: file("extra.txt", "text/plain", "TAIL"),
      name: "extra.txt",
      kind: "file",
    })
    const edited = await buildEditedMessageParts(original, draft.text, remaining)
    expect(edited.map((part) => part.type)).toEqual([
      "image_url",
      "text",
      "unknown",
      "text",
      "file",
      "file",
    ])
    expect(edited[0].meta.source_name).toBe("replaced.png")
    expect(edited[0].image_url.url).toMatch(/^data:image\/png;base64,/)
    expect(edited[4].file).toMatchObject({ name: "note.txt", path: "/workspace/other.txt" })
    expect(edited[5].file).toMatchObject({ name: "extra.txt", content: "TAIL" })
    expect(original[2].file.path).toBe("/workspace/note.txt")
  })

  it("rejects a changed merged textarea even when called programmatically", async () => {
    const draft = contentToEditableDraft(original)
    await expect(buildEditedMessageParts(original, "rewritten", draft.attachments)).rejects.toThrow(
      /merged text/i,
    )
    expect(original[4].text).toBe("second")
  })

  it("leaves the plain composer prepend-text helper unchanged", async () => {
    const parts = await buildMessageParts(" hello ", [
      { file: file("a.txt", "text/plain", "A"), name: "a.txt", kind: "file" },
    ])
    expect(parts).toEqual([
      { type: "text", text: " hello " },
      expect.objectContaining({ type: "file", file: expect.objectContaining({ content: "A" }) }),
    ])
  })

  it("appends entered caption when the array source has no original text part", async () => {
    const source = [
      {
        type: "image_url",
        image_url: { url: "file:///tmp/only.png" },
        meta: { source_name: "only.png", vendor: "keep" },
      },
    ]
    const draft = contentToEditableDraft(source)
    expect(draft.text).toBe("")
    expect(draft.textRestricted).toBe(false)
    const edited = await buildEditedMessageParts(
      draft.sourceContent,
      "a caption",
      draft.attachments,
    )
    expect(edited).toEqual([
      {
        type: "image_url",
        image_url: { url: "file:///tmp/only.png" },
        meta: { source_name: "only.png", vendor: "keep" },
      },
      { type: "text", text: "a caption" },
    ])
    expect(source).toHaveLength(1)
  })

  it("keeps a caption after deleting the sole image of an image-only message", async () => {
    const source = [
      {
        type: "image_url",
        image_url: { url: "file:///tmp/only.png" },
        meta: { source_name: "only.png" },
      },
    ]
    const edited = await buildEditedMessageParts(
      contentToEditableDraft(source).sourceContent,
      "just text",
      [],
    )
    expect(edited).toEqual([{ type: "text", text: "just text" }])
  })

  it("deep-snapshots sourceContent so later mutate/reorder of the original is ignored", async () => {
    const source = [
      {
        type: "image_url",
        image_url: { url: "file:///tmp/pic.png" },
        meta: { source_name: "pic.png", vendor: "keep" },
      },
      { type: "unknown", payload: { raw: 1 }, meta: { vendor: "x" } },
      { type: "text", text: "hello", extra: { keep: true } },
      { type: "file", file: { name: "note.txt", path: "/workspace/note.txt" } },
    ]
    const draft = contentToEditableDraft(source)
    expect(draft.sourceContent).not.toBe(source)
    expect(draft.sourceContent[0]).not.toBe(source[0])
    expect(draft.attachments.map((item) => item.originalIndex)).toEqual([0, 3])
    source.reverse()
    source[0].file.path = "/mutated"
    source[3].meta.vendor = "mutated"
    const edited = await buildEditedMessageParts(draft.sourceContent, "world", draft.attachments)
    expect(edited).toEqual([
      {
        type: "image_url",
        image_url: { url: "file:///tmp/pic.png" },
        meta: { source_name: "pic.png", vendor: "keep" },
      },
      { type: "unknown", payload: { raw: 1 }, meta: { vendor: "x" } },
      { type: "text", text: "world", extra: { keep: true } },
      { type: "file", file: { name: "note.txt", path: "/workspace/note.txt" } },
    ])
  })
})
