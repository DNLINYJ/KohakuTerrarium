const MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
const MAX_IMAGE_BYTES = 5 * 1024 * 1024

function clonePart(part) {
  return JSON.parse(JSON.stringify(part || {}))
}

function cloneContent(content) {
  if (typeof content === "string") return content
  if (!Array.isArray(content)) return content
  return JSON.parse(JSON.stringify(content))
}

function partName(part) {
  if (part?.type === "image_url") {
    return part.meta?.source_name || part.meta?.revised_prompt || "image"
  }
  if (part?.type === "file") {
    return part.file?.name || part.file?.path || "file"
  }
  return "attachment"
}

export function formatBytes(bytes) {
  if (bytes >= 1024 * 1024) return (bytes / (1024 * 1024)).toFixed(1) + " MB"
  if (bytes >= 1024) return (bytes / 1024).toFixed(1) + " KB"
  return bytes + " B"
}

const FILE_PLACEHOLDER_RE = /\[\[file:([^\]]+)\]\]/g
export function parseFilePlaceholderOrdinal(ref, fileCount) {
  if (!/^\p{Decimal_Number}+$/u.test(ref)) return null
  let value = 0
  for (const ch of ref) {
    const code = ch.codePointAt(0)
    let zero = code
    while (/\p{Decimal_Number}/u.test(String.fromCodePoint(zero - 1))) zero--
    value = value * 10 + ((code - zero) % 10)
    if (value >= fileCount) return null
  }
  return value
}

function placeholderRefsInTexts(texts) {
  const refs = []
  for (const text of texts) {
    if (typeof text !== "string" || !text) continue
    FILE_PLACEHOLDER_RE.lastIndex = 0
    let match
    while ((match = FILE_PLACEHOLDER_RE.exec(text))) refs.push(match[1])
  }
  return refs
}

function assertNumericPlaceholderBindings(oldTexts, newTexts, oldFiles, newFiles) {
  const oldRefs = new Set(placeholderRefsInTexts(oldTexts))
  const newRefs = new Set(placeholderRefsInTexts(newTexts))
  const shared = [...oldRefs].filter((ref) => newRefs.has(ref))
  const resolve = (ref, files) => {
    const idx = parseFilePlaceholderOrdinal(ref, files.length)
    return idx == null ? null : files[idx]
  }
  for (const ref of shared) {
    if (resolve(ref, oldFiles) !== resolve(ref, newFiles)) {
      throw new Error("Attachment changes would rebind an existing numeric file reference")
    }
  }
}

function isAttachmentPart(part) {
  return part?.type === "image_url" || part?.type === "file"
}

function mergedTextFromContent(content) {
  return content
    .filter((part) => part?.type === "text")
    .map((part) => part.text || "")
    .join("\n")
}

export function contentToEditableDraft(content) {
  if (typeof content === "string") {
    return {
      text: content,
      attachments: [],
      sourceContent: content,
      textRestricted: false,
    }
  }
  if (!Array.isArray(content)) {
    return { text: "", attachments: [], sourceContent: content, textRestricted: false }
  }

  const sourceContent = cloneContent(content)
  const textParts = sourceContent.filter((part) => part?.type === "text")
  const attachments = []
  sourceContent.forEach((part, originalIndex) => {
    if (!isAttachmentPart(part)) return
    attachments.push({
      id: `existing_${originalIndex}_${partName(part)}`,
      name: partName(part),
      kind: part.type === "image_url" ? "image" : "file",
      originalIndex,
      existingPart: clonePart(part),
    })
  })

  return {
    text: mergedTextFromContent(sourceContent),
    attachments,
    sourceContent,
    textRestricted: textParts.length > 1,
  }
}

export async function buildEditedMessageParts(sourceContent, draftText, attachments = []) {
  if (typeof sourceContent === "string" || !Array.isArray(sourceContent)) {
    return buildMessageParts(draftText, attachments)
  }

  const textParts = []
  sourceContent.forEach((part, originalIndex) => {
    if (part?.type === "text") textParts.push({ part, originalIndex })
  })
  const merged = mergedTextFromContent(sourceContent)
  const textRestricted = textParts.length > 1
  if (textRestricted && draftText !== merged) {
    throw new Error("Merged text editing is disabled for multi-text messages")
  }

  const keptByOriginal = new Map()
  const appended = []
  for (const attachment of attachments) {
    if (Number.isInteger(attachment?.originalIndex) && attachment.originalIndex >= 0) {
      keptByOriginal.set(attachment.originalIndex, attachment)
    } else {
      appended.push(attachment)
    }
  }

  const parts = []
  const newFiles = []
  for (let originalIndex = 0; originalIndex < sourceContent.length; originalIndex++) {
    const part = sourceContent[originalIndex]
    if (part?.type === "text") {
      if (textRestricted) {
        parts.push(clonePart(part))
        continue
      }
      const next = clonePart(part)
      next.text = typeof draftText === "string" ? draftText : part.text || ""
      parts.push(next)
      continue
    }
    if (isAttachmentPart(part)) {
      const replacement = keptByOriginal.get(originalIndex)
      if (!replacement) continue
      let nextPart
      if (replacement.existingPart && !replacement.file) {
        nextPart = clonePart(replacement.existingPart)
      } else {
        nextPart = await attachmentToPart(replacement)
      }
      if (nextPart?.type === "file") newFiles.push(originalIndex)
      parts.push(nextPart)
      continue
    }
    parts.push(clonePart(part))
  }
  for (const attachment of appended) {
    const part = await attachmentToPart(attachment)
    if (part?.type === "file") newFiles.push(Symbol("new file"))
    parts.push(part)
  }
  if (
    !textRestricted &&
    textParts.length === 0 &&
    typeof draftText === "string" &&
    draftText.trim()
  ) {
    parts.push({ type: "text", text: draftText })
  }
  const oldFiles = []
  sourceContent.forEach((part, originalIndex) => {
    if (part?.type === "file") oldFiles.push(originalIndex)
  })
  const oldTexts = sourceContent
    .filter((part) => part?.type === "text")
    .map((part) => part.text || "")
  const newTexts = parts.filter((part) => part?.type === "text").map((part) => part.text || "")
  assertNumericPlaceholderBindings(oldTexts, newTexts, oldFiles, newFiles)
  return parts
}

export async function imageFileToPart(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      resolve({
        type: "image_url",
        image_url: { url: reader.result, detail: "low" },
        meta: { source_type: "attachment", source_name: file.name },
      })
    }
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}

async function fileToBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => {
      const result = typeof reader.result === "string" ? reader.result : ""
      resolve(result.includes(",") ? result.split(",", 2)[1] : result)
    }
    reader.onerror = () => reject(reader.error)
    reader.readAsDataURL(file)
  })
}

export async function genericFileToPart(file) {
  const mime = file.type || "application/octet-stream"
  const textLike =
    mime.startsWith("text/") ||
    ["application/json", "application/xml", "application/javascript", "image/svg+xml"].includes(
      mime,
    ) ||
    /\.(md|txt|py|js|ts|tsx|jsx|json|yaml|yml|toml|ini|cfg|csv|tsv|html|css|xml|sh|rs|go|java|c|cc|cpp|h|hpp)$/i.test(
      file.name,
    )

  if (textLike) {
    return {
      type: "file",
      file: {
        name: file.name,
        mime,
        path: null,
        content: await file.text(),
        data_base64: null,
        encoding: "utf-8",
        is_inline: true,
      },
    }
  }

  return {
    type: "file",
    file: {
      name: file.name,
      mime,
      path: null,
      content: null,
      data_base64: await fileToBase64(file),
      encoding: "base64",
      is_inline: true,
    },
  }
}

export function detectAttachmentKind(file) {
  return (file?.type || "").startsWith("image/") ? "image" : "file"
}

export function validateAttachment(file, kind = detectAttachmentKind(file), options = {}) {
  const maxAttachmentBytes = options.maxAttachmentBytes ?? MAX_ATTACHMENT_BYTES
  const maxImageBytes = options.maxImageBytes ?? MAX_IMAGE_BYTES
  const limit = kind === "image" ? maxImageBytes : maxAttachmentBytes
  if (file.size > limit) {
    return { code: "too-large", name: file.name, size: file.size, limit, kind }
  }
  if (kind === "image" && file.type && !file.type.startsWith("image/")) {
    return { code: "not-image", name: file.name, kind, type: file.type }
  }
  return null
}

export function validateAttachments(files, options = {}) {
  const attachments = []
  const errors = []
  for (const file of Array.from(files || [])) {
    const kind = options.kind || detectAttachmentKind(file)
    const error = validateAttachment(file, kind, options)
    if (error) errors.push(error)
    else attachments.push({ file, name: file.name, kind })
  }
  return { attachments, errors }
}

export async function attachmentToPart(attachment) {
  if (attachment?.existingPart) return clonePart(attachment.existingPart)
  if (attachment?.kind === "image") return imageFileToPart(attachment.file)
  return genericFileToPart(attachment.file)
}

export async function buildMessageParts(text, attachments = []) {
  const parts = []
  if (typeof text === "string" && text.trim()) {
    parts.push({ type: "text", text })
  }
  for (const attachment of attachments) {
    parts.push(await attachmentToPart(attachment))
  }
  return parts
}

export { MAX_ATTACHMENT_BYTES, MAX_IMAGE_BYTES }
