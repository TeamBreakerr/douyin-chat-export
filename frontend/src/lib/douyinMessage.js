// Douyin IM message parsing/detection — pure functions of a message row.
// Extracted from MessageList.vue so the intricate payload heuristics can be
// unit-tested and reused. Nothing here depends on Vue reactivity or the DOM.
//
// The message shape: { msg_id, msg_type, content, raw_data (JSON string with a
// (often double-encoded) content_json), sender_uid, sender_name, timestamp,
// media_local_path, media_url, ref_msg }.

// Cache parsed content_json by msg_id (cleared on conversation switch/jump).
const cjCache = new Map()

export function clearCjCache() {
  cjCache.clear()
}

export function getContentJson(msg) {
  if (cjCache.has(msg.msg_id)) return cjCache.get(msg.msg_id)
  let cj = null
  try {
    if (msg.raw_data) {
      const raw = typeof msg.raw_data === 'string' ? JSON.parse(msg.raw_data) : msg.raw_data
      if (raw.content_json) {
        cj = typeof raw.content_json === 'string' ? JSON.parse(raw.content_json) : raw.content_json
      }
    }
  } catch {}
  cjCache.set(msg.msg_id, cj)
  return cj
}

export function tryParseJson(str) {
  if (!str || !str.startsWith('{')) return null
  try { return JSON.parse(str) } catch { return null }
}

export function tryParseShareContent(content) {
  if (!content || !content.startsWith('{')) return null
  try {
    const obj = JSON.parse(content)
    if (obj.content_title || obj.cover_url) return obj
  } catch {}
  return null
}

export function extractShareTitle(content) {
  if (!content) return ''
  // "分享[商品]: 商品名称" / "分享[视频]: 标题" / "[分享视频]标题"
  const m = content.match(/^(?:分享\[.+?\][:：]\s*|^\[分享视频\])(.+)/s)
  return m ? m[1].trim() : ''
}

// msg_type=1 but actually a system-tip JSON (has tips, but not a sticker)
export function isJsonSystemMsg(msg) {
  if (msg.msg_type !== 1) return false
  if (!msg.content || !msg.content.startsWith('{')) return false
  if (isJsonSticker(msg)) return false  // 贴纸优先
  return msg.content.includes('"tips"') && msg.content.includes('"aweType"')
}

// msg_type=1 but actually a sticker JSON (content may be truncated → also check content_json)
export function isJsonSticker(msg) {
  if (msg.msg_type !== 1) return false
  if (!msg.content || !msg.content.startsWith('{')) return false
  if (msg.content.includes('"stickers"') || msg.content.includes('"joker_stickers"')) return true
  const cj = getContentJson(msg)
  if (cj && (cj.stickers || cj.joker_stickers)) return true
  return false
}

export function getStickerUrl(msg) {
  const cj = getContentJson(msg)
  const source = cj || tryParseShareContent(msg.content)
  if (!source) return null
  if (source.stickers?.length > 0) {
    return source.stickers[0].static_url?.url_list?.[0] || null
  }
  if (source.joker_stickers?.length > 0) {
    return source.joker_stickers[0].static_url?.url_list?.[0] || null
  }
  return null
}

// "一起看视频" 邀请卡片 (aweType=9000)：msg_type=0，但不是普通系统提示，
// 单独渲染成卡片。返回 {title, subtitle, cover} 或 null。
export function getWatchTogether(msg) {
  const cj = getContentJson(msg) || tryParseJson(msg.content)
  if (!cj || cj.aweType !== 9000) return null
  return {
    title: cj.title || '一起看视频',
    subtitle: cj.sub_title || cj.hint || '',
    cover: cj.cover_url?.url_list?.[0] || '',
  }
}

// Whether a message should render at all (empty system messages are hidden).
export function shouldShow(msg) {
  if (getWatchTogether(msg)) return true       // 一起看视频卡片始终显示
  if (msg.msg_type === 0) return !!renderSystemMsg(msg)
  if (isJsonSystemMsg(msg)) return !!renderSystemMsg(msg)
  return true
}

// System message: render the template (prefer content_json — content may be truncated).
export function renderSystemMsg(msg) {
  const cj = getContentJson(msg)
  const source = cj || tryParseJson(msg.content)
  if (!source) {
    return (msg.content && msg.content !== '{}') ? msg.content : ''
  }
  if (source.tips) {
    let text = source.tips
    if (source.template) {
      for (const t of source.template) {
        text = text.replace(`{{${t.key}}}`, t.name || '')
      }
    }
    return text
  }
  if (source.hint_text) return source.hint_text
  if (Object.keys(source).length <= 1) return ''
  // 兜底：不认识的 JSON 卡片消息不要把原始 JSON 吐到界面上；只回显纯文本 content。
  return (msg.content && msg.content !== '{}' && !msg.content.startsWith('{')) ? msg.content : ''
}

// Extract server_message_id from the raw JSON string (avoids JSON.parse losing
// >2^53 integer precision); tolerates escaped quotes in doubly-encoded raw_data.
export function extractServerMsgIds(msg) {
  const ids = []
  try {
    const raw = typeof msg.raw_data === 'string' ? msg.raw_data : JSON.stringify(msg.raw_data)
    const re = /server_message_id\\?"?\s*:\s*(\d{15,})/g
    let match
    while ((match = re.exec(raw)) !== null) {
      ids.push(match[1])
    }
  } catch {}
  return ids
}

// Comment-quoting-a-video message (aweType=700 + related_share_video)
export function isVideoComment(msg) {
  const cj = getContentJson(msg)
  if (!cj) return false
  return cj.aweType === 700 && !!cj.related_share_video?.itemId
}

// Whether a msg_type=1 message is actually a share card (JSON content with content_title)
export function isJsonShare(msg) {
  if (msg.msg_type === 4) return false
  if (!msg.content || !msg.content.startsWith('{')) return false
  return msg.content.includes('content_title') || msg.content.includes('cover_url')
}

// Share-card info extraction (video share, product card, quoted-video comment).
export function getShareInfo(msg) {
  const cj = getContentJson(msg)
  const source = cj || tryParseShareContent(msg.content)
  if (!source) return { title: '', author: '', cover: '', itemId: '', productUrl: '', comment: '', commentUser: '' }

  // 商品卡片 (aweType=11029): 从 im_dynamic_patch.raw_data 提取
  const patch = source.im_dynamic_patch
  if (patch?.raw_data) {
    try {
      const pr = typeof patch.raw_data === 'string' ? JSON.parse(patch.raw_data) : patch.raw_data
      const title = pr.content_top?.content || extractShareTitle(msg.content) || ''
      const cover = pr.top?.content || ''
      let productUrl = ''
      const actions = pr.whole_card?.action_info
      if (actions?.[0]?.params?.schema) {
        const m = actions[0].params.schema.match(/commodity_id=(\d+)/)
        if (m) productUrl = 'https://www.douyin.com/product/' + m[1]
      }
      return { title, author: '', cover, itemId: '', productUrl, comment: '', commentUser: '', commentImg: '' }
    } catch {}
  }

  // aweType=10500: 引用视频评论 (comment 字段); aweType=700: (text 字段)
  const comment = source.comment || source.text || ''
  const commentUser = source.comment_user_name || ''
  const commentImg = source.comment_url?.url_list?.[0] || ''
  const relatedVideo = source.related_share_video || {}
  return {
    title: source.content_title || source.aweme_title || extractShareTitle(msg.content) || '',
    author: source.content_name || '',
    cover: source.cover_url?.url_list?.[0] || '',
    itemId: source.itemId || relatedVideo.itemId || '',
    productUrl: '',
    comment,
    commentUser,
    commentImg,
  }
}

// Image inline_pic base64 (WebP thumbnail); strip embedded newlines from the base64.
export function getInlinePic(msg) {
  const cj = getContentJson(msg)
  if (cj?.inline_pic) {
    return 'data:image/webp;base64,' + cj.inline_pic.replace(/\r?\n/g, '')
  }
  return null
}

// msg_type=3 whose local file is an .mp4 (a real downloaded video, not an image)
export function isVideoMsg(msg) {
  return msg.media_local_path && /\.mp4$/i.test(msg.media_local_path)
}

// Alias: a JSON-video message has a playable local .mp4 (same test).
export const hasLocalVideo = isVideoMsg

// JSON video message (msg_type=5, or legacy msg_type=1 with cj.video.vid) — poster only.
export function isJsonVideo(msg) {
  if (msg.msg_type === 5) return true
  if (msg.msg_type !== 1) return false
  const cj = getContentJson(msg)
  return !!(cj && cj.video && cj.video.vid)
}

// Video poster: the inline_pic base64 WebP thumbnail.
export function getVideoPoster(msg) {
  return getInlinePic(msg)
}

// Video duration in seconds (rendered with a ″ suffix).
export function getVideoDuration(msg) {
  const cj = getContentJson(msg)
  const d = cj?.duration
  if (d === undefined || d === null) return ''
  const n = typeof d === 'string' ? parseFloat(d) : Number(d)
  if (!n || isNaN(n)) return ''
  return Math.round(n) + '″'
}

// Image src: local original > inline_pic thumbnail (video goes through its own branch).
export function getImageSrc(msg) {
  if (msg.media_local_path && !isVideoMsg(msg)) return '/media/' + msg.media_local_path
  return getInlinePic(msg)
}

// Emoji src: local > CDN URL.
export function getEmojiSrc(msg) {
  if (msg.media_local_path) return '/media/' + msg.media_local_path
  return msg.media_url || null
}

// Recalled-message detection.
export function isRecalled(msg) {
  const cj = getContentJson(msg)
  if (cj?.is_recalled) return true
  if (!msg.raw_data) return false
  try {
    const raw = typeof msg.raw_data === 'string' ? JSON.parse(msg.raw_data) : msg.raw_data
    return !!raw.is_recalled
  } catch { return false }
}

// Voice-message detection (msg_type stays 0/other; identified by resource_url).
export function isVoiceMsg(msg) {
  const cj = getContentJson(msg)
  if (cj?.resource_url?.url_list?.length) return true
  if (msg.content?.startsWith('{') && msg.content.includes('resource_url')) {
    try { const o = JSON.parse(msg.content); return !!o.resource_url?.url_list?.length } catch {}
  }
  return false
}

export function getVoiceUrl(msg) {
  const cj = getContentJson(msg)
  // Guard JSON.parse: malformed content that merely starts with '{' must not
  // throw during render (matches getVoiceDuration below).
  const source = cj || (msg.content?.startsWith('{') ? (() => { try { return JSON.parse(msg.content) } catch { return null } })() : null)
  if (!source?.resource_url?.url_list?.length) return ''
  if (msg.media_local_path) return `/media/${msg.media_local_path}`
  return source.resource_url.url_list[0]
}

export function getVoiceDuration(msg) {
  const cj = getContentJson(msg)
  const source = cj || (msg.content?.startsWith('{') ? (() => { try { return JSON.parse(msg.content) } catch { return null } })() : null)
  if (!source?.duration) return '?'
  return Math.round(source.duration / 1000)
}

// Reply/quote parsing (new field-18 format + legacy formats).
export function getRefMsg(msg) {
  if (!msg.ref_msg) return null
  try {
    const ref = typeof msg.ref_msg === 'string' ? JSON.parse(msg.ref_msg) : msg.ref_msg
    if (ref.content || ref.nickname) return ref
    if (ref.server_id && String(ref.server_id).length >= 15) return ref
    if (ref.content_json) return ref
  } catch {}
  return null
}

export function getRefContent(ref) {
  if (!ref) return ''
  if (ref.content) return ref.content
  if (ref.refmsg_content) {
    try {
      const cj = JSON.parse(ref.refmsg_content)
      if (cj.text) return cj.text
    } catch {}
  }
  if (ref.content_json) {
    try {
      const cj = JSON.parse(ref.content_json)
      if (cj.text) return cj.text
      if (cj.content_title) return `[分享] ${cj.content_title}`
      if (cj.aweType === 501 || cj.aweType === 507) return '[表情]'
    } catch {}
    if (!ref.content_json.startsWith('{')) return ref.content_json
  }
  return '[消息]'
}

export function getRefNickname(ref) {
  if (!ref) return ''
  return ref.nickname || ''
}
