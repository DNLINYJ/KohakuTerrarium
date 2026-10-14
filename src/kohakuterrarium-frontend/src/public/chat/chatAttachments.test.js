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
import { parseFilePlaceholderOrdinal } from "./chatAttachments.js"

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

describe("numeric file placeholder ordinals", () => {
  it("normalizes decimal ordinals without Number overflow and rejects unsafe unicode", () => {
    expect(parseFilePlaceholderOrdinal("00", 1)).toBe(0)
    expect(parseFilePlaceholderOrdinal("٠٠", 1)).toBe(0)
    expect(parseFilePlaceholderOrdinal("００", 1)).toBe(0)
    expect(parseFilePlaceholderOrdinal("0٠０", 1)).toBe(0)
    expect(parseFilePlaceholderOrdinal("١", 2)).toBe(1)
    expect(parseFilePlaceholderOrdinal("߁", 2)).toBe(1)
    expect(parseFilePlaceholderOrdinal("𝟙", 2)).toBe(1)
    expect(parseFilePlaceholderOrdinal("²", 3)).toBeNull()
    expect(parseFilePlaceholderOrdinal("1a", 3)).toBeNull()
    expect(parseFilePlaceholderOrdinal("9".repeat(5000), 1)).toBeNull()
    expect(parseFilePlaceholderOrdinal("1", 1)).toBeNull()
    expect(parseFilePlaceholderOrdinal("0", 1)).toBe(0)
  })
})

describe("numeric file placeholder edit association", () => {
  const sameName = "dup.txt"

  function abcFiles(text = "see [[file:1]]") {
    return [
      { type: "text", text },
      { type: "file", file: { name: sameName, path: "/workspace/a.txt", content: "FILE-A" } },
      { type: "file", file: { name: sameName, path: "/workspace/b.txt", content: "FILE-B" } },
      { type: "file", file: { name: sameName, path: "/workspace/c.txt", content: "FILE-C" } },
    ]
  }

  function withoutOriginal(attachments, originalIndex) {
    return attachments.filter((item) => item.originalIndex !== originalIndex)
  }

  function replaceAtOriginal(attachments, originalIndex, next) {
    return attachments.map((item) =>
      item.originalIndex === originalIndex ? { ...item, ...next } : item,
    )
  }

  it("preserves extension fields that resemble internal association keys", async () => {
    const source = abcFiles()
    source[2].__fileIdentity = "persisted extension"
    const draft = contentToEditableDraft(source)
    expect(await buildEditedMessageParts(source, draft.text, draft.attachments)).toEqual(source)
  })

  it("rejects deleting an earlier same-name file that would rebind an unchanged [[file:1]]", async () => {
    const source = abcFiles()
    const draft = contentToEditableDraft(source)
    await expect(
      buildEditedMessageParts(source, draft.text, withoutOriginal(draft.attachments, 1)),
    ).rejects.toThrow(/numeric file reference|placeholder/i)
    expect(source[1].file.content).toBe("FILE-A")
  })

  it("rejects deleting the referenced same-name file", async () => {
    const source = abcFiles()
    const draft = contentToEditableDraft(source)
    await expect(
      buildEditedMessageParts(source, draft.text, withoutOriginal(draft.attachments, 2)),
    ).rejects.toThrow(/numeric file reference|placeholder/i)
  })

  it("allows deleting a file after the referenced ordinal", async () => {
    const source = abcFiles()
    const draft = contentToEditableDraft(source)
    const edited = await buildEditedMessageParts(
      source,
      draft.text,
      withoutOriginal(draft.attachments, 3),
    )
    expect(edited.map((part) => part.type)).toEqual(["text", "file", "file"])
    expect(edited[0].text).toBe("see [[file:1]]")
    expect(edited[1].file.content).toBe("FILE-A")
    expect(edited[2].file.content).toBe("FILE-B")
  })

  it("allows replacing the same occurrence with another file", async () => {
    const source = abcFiles()
    const draft = contentToEditableDraft(source)
    const edited = await buildEditedMessageParts(
      source,
      draft.text,
      replaceAtOriginal(draft.attachments, 2, {
        existingPart: undefined,
        file: file(sameName, "text/plain", "FILE-B2"),
        kind: "file",
        name: sameName,
      }),
    )
    expect(edited[0].text).toBe("see [[file:1]]")
    expect(edited[2].file.content).toBe("FILE-B2")
    expect(edited[1].file.content).toBe("FILE-A")
    expect(edited[3].file.content).toBe("FILE-C")
  })

  it("allows deleting images because they are not file ordinals", async () => {
    const source = [
      { type: "text", text: "head [[file:1]] tail" },
      {
        type: "image_url",
        image_url: { url: "file:///tmp/i.png" },
        meta: { source_name: "i.png" },
      },
      { type: "file", file: { name: sameName, path: "/workspace/a.txt", content: "FILE-A" } },
      { type: "file", file: { name: sameName, path: "/workspace/b.txt", content: "FILE-B" } },
    ]
    const draft = contentToEditableDraft(source)
    const edited = await buildEditedMessageParts(
      source,
      draft.text,
      withoutOriginal(draft.attachments, 1),
    )
    expect(edited.map((part) => part.type)).toEqual(["text", "file", "file"])
    expect(edited[0].text).toBe("head [[file:1]] tail")
    expect(edited[2].file.content).toBe("FILE-B")
  })

  it("rejects replacing a file with an image when that shifts later ordinals of an existing reference", async () => {
    const source = abcFiles()
    const draft = contentToEditableDraft(source)
    await expect(
      buildEditedMessageParts(
        source,
        draft.text,
        replaceAtOriginal(draft.attachments, 1, {
          existingPart: undefined,
          file: file("a.png", "image/png", "IMG"),
          kind: "image",
          name: "a.png",
        }),
      ),
    ).rejects.toThrow(/numeric file reference|placeholder/i)
  })

  it("rejects replacing an image with a file before an existing numeric target", async () => {
    const source = abcFiles()
    source.splice(1, 0, { type: "image_url", image_url: { url: "data:image/png;base64,YQ==" } })
    const draft = contentToEditableDraft(source)
    const editedAttachments = replaceAtOriginal(draft.attachments, 1, {
      existingPart: undefined,
      file: file("new.txt", "text/plain", "INSERTED"),
      kind: "file",
      name: "new.txt",
    })
    await expect(buildEditedMessageParts(source, draft.text, editedAttachments)).rejects.toThrow(
      /numeric file reference/i,
    )
  })

  it("rejects an unchanged out-of-range numeric token becoming bound after append", async () => {
    const source = [
      { type: "text", text: "see [[file:1]]" },
      { type: "file", file: { name: sameName, path: "/workspace/a.txt", content: "FILE-A" } },
    ]
    const draft = contentToEditableDraft(source)
    await expect(
      buildEditedMessageParts(source, draft.text, [
        ...draft.attachments,
        { file: file(sameName, "text/plain", "FILE-B"), name: sameName, kind: "file" },
      ]),
    ).rejects.toThrow(/numeric file reference|placeholder/i)
  })

  it("allows a deliberately edited numeric token to use the resulting ordinals", async () => {
    const source = abcFiles("see [[file:1]]")
    const draft = contentToEditableDraft(source)
    const edited = await buildEditedMessageParts(
      source,
      "now [[file:0]]",
      withoutOriginal(draft.attachments, 1),
    )
    expect(edited[0].text).toBe("now [[file:0]]")
    expect(edited.map((part) => part.type)).toEqual(["text", "file", "file"])
    expect(edited[1].file.content).toBe("FILE-B")
    expect(edited[2].file.content).toBe("FILE-C")
  })

  it("does not treat superscript file names as decimal ordinals", async () => {
    const source = [
      { type: "text", text: "see [[file:²]]" },
      { type: "file", file: { name: sameName, path: "/workspace/a.txt", content: "FILE-A" } },
      { type: "file", file: { name: "²", path: "/workspace/b.txt", content: "FILE-B" } },
    ]
    const draft = contentToEditableDraft(source)
    const edited = await buildEditedMessageParts(
      source,
      draft.text,
      withoutOriginal(draft.attachments, 1),
    )
    expect(edited[1].file.name).toBe("²")
    expect(edited[0].text).toBe(source[0].text)
  })

  it("does not rewrite readonly multi-text when an earlier file deletion would rebind a later numeric token", async () => {
    const source = [
      { type: "text", text: "keep" },
      { type: "file", file: { name: sameName, path: "/workspace/a.txt", content: "FILE-A" } },
      { type: "text", text: "see [[file:1]]" },
      { type: "file", file: { name: sameName, path: "/workspace/b.txt", content: "FILE-B" } },
      { type: "file", file: { name: sameName, path: "/workspace/c.txt", content: "FILE-C" } },
    ]
    const draft = contentToEditableDraft(source)
    expect(draft.textRestricted).toBe(true)
    await expect(
      buildEditedMessageParts(source, draft.text, withoutOriginal(draft.attachments, 1)),
    ).rejects.toThrow(/numeric file reference|placeholder/i)
    expect(source[2].text).toBe("see [[file:1]]")
  })
})
