// Shared message-action strings for the one production message row
// (``MessageRow.vue``) that both hosts render. Merged into each locale by
// ``./index.js`` so a copy failure is surfaced in the reader's language rather
// than as an English-only toast, and the English table remains the fallback.
export default {
  en: {
    "chat.editUnsupportedParts":
      "This message contains unsupported content blocks and cannot be edited. The original history is unchanged.",
    "chat.copyFailed": "Copy failed — the clipboard is not available.",
    "chat.copied": "Copied to clipboard",
    "chat.replaceAttachment": ({ name }) => `Replace ${name}`,
    "chat.removeAttachment": ({ name }) => `Remove ${name}`,
    "chat.editMergedTextDisabled":
      "This message has multiple interleaved text blocks. Merged text editing is disabled; you can still change attachments or save unchanged.",
  },
  "zh-CN": {
    "chat.editUnsupportedParts": "此消息含有尚不支持的内容块，无法编辑。原始历史保持不变。",
    "chat.copyFailed": "复制失败 — 剪贴板不可用。",
    "chat.copied": "已复制到剪贴板",
    "chat.replaceAttachment": ({ name }) => `替换 ${name}`,
    "chat.removeAttachment": ({ name }) => `移除 ${name}`,
    "chat.editMergedTextDisabled":
      "此消息含有多段交错文本。已禁用合并文本编辑；仍可修改附件或原样保存。",
  },
  "zh-TW": {
    "chat.editUnsupportedParts": "此訊息含有尚未支援的內容區塊，無法編輯。原始歷史保持不變。",
    "chat.copyFailed": "複製失敗 — 剪貼簿無法使用。",
    "chat.copied": "已複製到剪貼簿",
    "chat.replaceAttachment": ({ name }) => `取代 ${name}`,
    "chat.removeAttachment": ({ name }) => `移除 ${name}`,
    "chat.editMergedTextDisabled":
      "此訊息含有多段交錯文字。已停用合併文字編輯；仍可修改附件或原樣儲存。",
  },
}
