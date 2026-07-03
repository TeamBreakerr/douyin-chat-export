<template>
  <div class="msg-panel">
    <div v-if="!conversation" class="msg-empty">
      <div class="msg-empty-icon">💬</div>
      <div>选择一个会话查看聊天记录</div>
    </div>
    <template v-else>
      <div class="msg-header">
        <h3>{{ conversation.name || '未命名' }}</h3>
        <span class="msg-total">{{ total }} 条消息</span>
        <button v-if="!selfUid && senders.length === 2" class="msg-pick-self" @click="showPicker = true">
          设置"我"
        </button>
        <button v-if="selfUid" class="msg-pick-self picked" @click="showPicker = true">
          我: {{ selfUid.slice(-4) }}
        </button>
      </div>

      <!-- UID 选择弹窗 -->
      <div v-if="showPicker" class="picker-overlay" @click.self="showPicker = false">
        <div class="picker-dialog">
          <div class="picker-title">选择哪个是你自己</div>
          <div class="picker-hint">选择后消息会区分左右显示</div>
          <div
            v-for="s in senders"
            :key="s.sender_uid"
            class="picker-option"
            :class="{ active: selfUid === s.sender_uid }"
            @click="pickSelf(s.sender_uid)"
          >
            <span class="picker-uid">{{ userCache[s.sender_uid]?.nickname || ('UID ...' + s.sender_uid.slice(-6)) }}</span>
            <span class="picker-count">{{ s.msg_count }} 条消息</span>
          </div>
          <button v-if="selfUid" class="picker-clear" @click="pickSelf(null)">清除选择</button>
        </div>
      </div>

      <button v-if="hasMore && atLatest" class="msg-jump-fab msg-jump-top" @click="jumpToTop">
        ↑ 最早消息
      </button>
      <button v-if="!atLatest" class="msg-jump-fab msg-jump-bottom" @click="jumpToBottom">
        ↓ 最新消息
      </button>
      <div class="msg-list" ref="listRef">
        <div v-if="loading" class="msg-loading">加载中...</div>
        <div v-if="hasMore && !loading && atLatest" class="msg-load-more" @click="loadMore">
          ⬆ 加载更早消息
        </div>
        <template v-for="msg in messages" :key="msg.msg_id">
        <div
          v-if="shouldShow(msg)"
          class="msg-item"
          :data-msgid="msg.msg_id"
          :class="{
            'msg-self': isSelf(msg),
            'msg-system': (msg.msg_type === 0 && !isVoiceMsg(msg)) || isJsonSystemMsg(msg),
            'msg-highlight': highlightMsgId === msg.msg_id,
          }"
        >
          <!-- 系统消息居中显示 -->
          <template v-if="(msg.msg_type === 0 && !isVoiceMsg(msg)) || isJsonSystemMsg(msg)">
            <div class="msg-system-block">
              <div class="msg-system-text" @contextmenu="selectSystemContent">{{ renderSystemMsg(msg) }}</div>
              <!-- 引用的分享视频卡片 -->
              <div
                v-if="sysRefCache[msg.msg_id]"
                class="msg-system-ref"
                @click="openVideoById(sysRefCache[msg.msg_id].itemId)"
              >
                <img
                  v-if="sysRefCache[msg.msg_id].cover"
                  :src="sysRefCache[msg.msg_id].cover"
                  class="msg-system-ref-cover"
                  @error="onImgError"
                />
                <span class="msg-system-ref-title">{{ sysRefCache[msg.msg_id].title }}</span>
              </div>
            </div>
          </template>
          <!-- 普通消息 -->
          <template v-else>
            <div class="msg-avatar" :class="{ 'msg-avatar-img': getAvatarUrl(msg) }">
              <img v-if="getAvatarUrl(msg)" :src="getAvatarUrl(msg)" @error="onImgError" />
              <span v-else :style="{ background: isSelf(msg) ? 'var(--accent)' : 'var(--bg-tertiary)', color: isSelf(msg) ? '#fff' : 'var(--text-secondary)' }">{{ displayName(msg)[0] }}</span>
            </div>
            <div class="msg-body" @contextmenu="selectMsgContent">
              <div class="msg-sender">{{ displayName(msg) }}</div>
              <!-- 引用/回复消息 -->
              <div v-if="getRefMsg(msg)" class="msg-ref-quote" @click="jumpToRefMsg(getRefMsg(msg))">
                <span v-if="getRefNickname(getRefMsg(msg))" class="msg-ref-name">{{ getRefNickname(getRefMsg(msg)) }}：</span>
                <span class="msg-ref-content">{{ getRefContent(getRefMsg(msg)) }}</span>
              </div>
              <!-- 表情包 -->
              <div v-if="msg.msg_type === 2 && getEmojiSrc(msg)" class="msg-media">
                <img :src="getEmojiSrc(msg)" :alt="msg.content" loading="lazy" @click="openLightbox(getEmojiSrc(msg))" @error="onImgError" />
              </div>
              <!-- 图片/视频：优先本地原文件，回退到 inline_pic 缩略图 -->
              <div v-else-if="msg.msg_type === 3" class="msg-media">
                <video
                  v-if="isVideoMsg(msg)"
                  :src="'/media/' + msg.media_local_path"
                  controls
                  preload="metadata"
                  :poster="getInlinePic(msg)"
                />
                <img
                  v-else-if="getImageSrc(msg)"
                  :src="getImageSrc(msg)"
                  alt="图片"
                  loading="lazy"
                  @click="openLightbox(getImageSrc(msg))"
                />
                <div v-else class="msg-media-missing">[图片已失效]</div>
              </div>
              <!-- 分享卡片 -->
              <div v-else-if="msg.msg_type === 4" class="msg-share-card" @click="openShare(msg)">
                <div v-if="getShareInfo(msg).comment || getShareInfo(msg).commentImg" class="msg-share-comment">
                  <span v-if="getShareInfo(msg).commentUser" class="msg-share-comment-user">{{ getShareInfo(msg).commentUser }}：</span>{{ getShareInfo(msg).comment }}
                  <img v-if="getShareInfo(msg).commentImg" :src="getShareInfo(msg).commentImg" class="msg-share-comment-img" loading="lazy" @error="onImgError" />
                </div>
                <div class="msg-share-card-inner">
                  <div class="msg-share-card-body">
                    <div class="msg-share-card-title">{{ getShareInfo(msg).title || '[分享]' }}</div>
                    <div v-if="getShareInfo(msg).author" class="msg-share-card-author">
                      @ {{ getShareInfo(msg).author }}
                    </div>
                  </div>
                  <img
                    v-if="getShareInfo(msg).cover"
                    :src="getShareInfo(msg).cover"
                    class="msg-share-card-cover"
                    loading="lazy"
                    @error="onImgError"
                  />
                </div>
              </div>
              <!-- 视频消息：本地有 .mp4 → 真播放；否则展示封面 + 时长 -->
              <div v-else-if="isJsonVideo(msg)" class="msg-media msg-video-poster">
                <video
                  v-if="hasLocalVideo(msg)"
                  :src="'/media/' + msg.media_local_path"
                  :poster="getVideoPoster(msg)"
                  controls
                  preload="none"
                  class="msg-video-player"
                />
                <template v-else>
                  <img
                    v-if="getVideoPoster(msg)"
                    :src="getVideoPoster(msg)"
                    loading="lazy"
                    @click="openLightbox(getVideoPoster(msg))"
                  />
                  <div v-else class="msg-media-missing">[视频]</div>
                  <div class="msg-video-overlay">
                    <span class="msg-video-play">▶</span>
                    <span v-if="getVideoDuration(msg)" class="msg-video-dur">{{ getVideoDuration(msg) }}</span>
                  </div>
                </template>
              </div>
              <!-- msg_type=1 但实际是贴纸/表情 JSON -->
              <div v-else-if="isJsonSticker(msg)" class="msg-media">
                <img v-if="getStickerUrl(msg)" :src="getStickerUrl(msg)" loading="lazy" @error="onImgError" />
                <div v-else class="msg-media-missing">[贴纸]</div>
              </div>
              <!-- msg_type=1 但实际是分享卡片（JSON content 含 content_title） -->
              <div v-else-if="isJsonShare(msg)" class="msg-share-card" @click="openShare(msg)">
                <div v-if="getShareInfo(msg).comment || getShareInfo(msg).commentImg" class="msg-share-comment">
                  <span v-if="getShareInfo(msg).commentUser" class="msg-share-comment-user">{{ getShareInfo(msg).commentUser }}：</span>{{ getShareInfo(msg).comment }}
                  <img v-if="getShareInfo(msg).commentImg" :src="getShareInfo(msg).commentImg" class="msg-share-comment-img" loading="lazy" @error="onImgError" />
                </div>
                <div class="msg-share-card-inner">
                  <div class="msg-share-card-body">
                    <div class="msg-share-card-title">{{ getShareInfo(msg).title || '[分享]' }}</div>
                    <div v-if="getShareInfo(msg).author" class="msg-share-card-author">
                      @ {{ getShareInfo(msg).author }}
                    </div>
                  </div>
                  <img
                    v-if="getShareInfo(msg).cover"
                    :src="getShareInfo(msg).cover"
                    class="msg-share-card-cover"
                    loading="lazy"
                    @error="onImgError"
                  />
                </div>
              </div>
              <!-- 语音消息 -->
              <div v-else-if="isVoiceMsg(msg)" class="msg-voice">
                <audio controls preload="none" :src="getVoiceUrl(msg)"></audio>
                <span class="msg-voice-dur">{{ getVoiceDuration(msg) }}″</span>
              </div>
              <!-- 评论引用视频（aweType=700，文本+关联视频） -->
              <div v-else-if="isVideoComment(msg)" class="msg-share-card" @click="openShare(msg)">
                <div class="msg-share-comment" v-html="highlightText(msg.content)"></div>
                <div class="msg-share-card-inner msg-share-card-ref">
                  <span class="msg-share-card-ref-icon">▶</span>
                  <span class="msg-share-card-ref-text">引用的视频</span>
                </div>
              </div>
              <!-- 文本消息 -->
              <div v-else class="msg-bubble" v-html="highlightText(msg.content)"></div>
              <div class="msg-time">
                <span v-if="isRecalled(msg)" class="msg-recalled-tag">已撤回</span>
                {{ formatTime(msg.timestamp) }}
              </div>
            </div>
          </template>
        </div>
        </template>
        <div v-if="hasMore && !loading && !atLatest" class="msg-load-more" @click="loadMore">
          ⬇ 加载更新消息
        </div>
        <div v-if="messages.length === 0 && !loading" class="msg-no-data">
          暂无消息
        </div>
      </div>
    </template>

    <!-- Lightbox -->
    <div v-if="lightboxSrc" class="lightbox-overlay" @click.self="closeLightbox">
      <img class="lightbox-img" :src="lightboxSrc" @click.self="closeLightbox" />
      <button class="lightbox-close" @click="closeLightbox">×</button>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, watch, nextTick, onMounted, onUnmounted } from 'vue'
import { highlightText as _highlightText } from '@/lib/highlight'
import { resolveAvatarUrl } from '@/lib/media'

const props = defineProps({
  conversation: Object,
  searchHighlight: String,
  jumpToSeq: Number,
})
const emit = defineEmits(['jumped'])

const messages = ref([])
const total = ref(0)
const loading = ref(false)
let scrollLocked = false  // 防止程序化滚动触发无限加载
const hasMore = ref(false)
const atLatest = ref(true)  // 当前是否在查看最新消息
const listRef = ref(null)
const senders = ref([])
const selfUid = ref(localStorage.getItem('selfUid') || '')
const showPicker = ref(false)

// 用户信息缓存 { uid: { nickname, avatar_url, unique_id } }
const userCache = reactive({})

// content_json 解析缓存
const cjCache = new Map()

// 系统消息引用的分享视频缓存 { msg_id: { title, cover, itemId } }
const sysRefCache = reactive({})

function getContentJson(msg) {
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

function isSelf(msg) {
  if (!selfUid.value) return false
  return msg.sender_uid === selfUid.value
}

function displayName(msg) {
  if (selfUid.value && msg.sender_uid === selfUid.value) {
    const u = userCache[msg.sender_uid]
    return u?.nickname || '我'
  }
  const u = userCache[msg.sender_uid]
  if (u?.nickname) return u.nickname
  if (msg.sender_name && msg.sender_name !== '__self__') return msg.sender_name
  return props.conversation?.name || '对方'
}

function getAvatarUrl(msg) {
  return resolveAvatarUrl(userCache[msg.sender_uid]?.avatar_url)
}

async function fetchUserInfo(uid) {
  if (!uid || userCache[uid]) return
  try {
    const res = await fetch(`/api/users/${uid}`)
    if (res.ok) {
      const data = await res.json()
      userCache[uid] = data
    }
  } catch {}
}

async function loadUserInfoForMessages(msgList) {
  const uids = new Set()
  for (const msg of msgList) {
    if (msg.sender_uid && !userCache[msg.sender_uid]) uids.add(msg.sender_uid)
  }
  await Promise.all(Array.from(uids).map(uid => fetchUserInfo(uid)))
}

function pickSelf(uid) {
  selfUid.value = uid || ''
  if (uid) {
    localStorage.setItem('selfUid', uid)
  } else {
    localStorage.removeItem('selfUid')
  }
  showPicker.value = false
}

// msg_type=1 但实际是系统提示消息（JSON 含 tips，但不是贴纸消息）
function isJsonSystemMsg(msg) {
  if (msg.msg_type !== 1) return false
  if (!msg.content || !msg.content.startsWith('{')) return false
  if (isJsonSticker(msg)) return false  // 贴纸优先
  return msg.content.includes('"tips"') && msg.content.includes('"aweType"')
}

// msg_type=1 但实际是贴纸/表情 JSON（content 可能被截断，也检查 content_json）
function isJsonSticker(msg) {
  if (msg.msg_type !== 1) return false
  if (!msg.content || !msg.content.startsWith('{')) return false
  // 先检查 content
  if (msg.content.includes('"stickers"') || msg.content.includes('"joker_stickers"')) return true
  // content 被截断时，检查 content_json
  const cj = getContentJson(msg)
  if (cj && (cj.stickers || cj.joker_stickers)) return true
  return false
}

function getStickerUrl(msg) {
  // 优先用完整的 content_json
  const cj = getContentJson(msg)
  const source = cj || tryParseShareContent(msg.content)
  if (!source) return null
  // stickers 类型
  if (source.stickers?.length > 0) {
    return source.stickers[0].static_url?.url_list?.[0] || null
  }
  // joker_stickers 类型
  if (source.joker_stickers?.length > 0) {
    return source.joker_stickers[0].static_url?.url_list?.[0] || null
  }
  return null
}

// 是否显示此消息
function shouldShow(msg) {
  if (msg.msg_type === 0) return !!renderSystemMsg(msg)
  if (isJsonSystemMsg(msg)) return !!renderSystemMsg(msg)
  return true
}

// 系统消息：解析模板（优先用 content_json，因为 content 可能被截断）
function renderSystemMsg(msg) {
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
  // hint_text（贴纸消息附带的提示文本）
  if (source.hint_text) return source.hint_text
  // 空对象 {} 或无有效文本 → 隐藏
  if (Object.keys(source).length <= 1) return ''
  return (msg.content && msg.content !== '{}') ? msg.content : ''
}

// 从原始 JSON 字符串中提取 server_message_id（避免 JSON.parse 丢失大整数精度）
function extractServerMsgIds(msg) {
  const ids = []
  try {
    const raw = typeof msg.raw_data === 'string' ? msg.raw_data : JSON.stringify(msg.raw_data)
    // raw_data 中 JSON 转义后形如 server_message_id\":12345 或 server_message_id":12345
    const re = /server_message_id\\?"?\s*:\s*(\d{15,})/g
    let match
    while ((match = re.exec(raw)) !== null) {
      ids.push(match[1])
    }
  } catch {}
  return ids
}

// 系统消息引用的视频：异步加载分享消息的标题和封面
async function loadSysRefs(msgList) {
  for (const msg of msgList) {
    if (msg.msg_type !== 0 || sysRefCache[msg.msg_id]) continue
    const smids = extractServerMsgIds(msg)
    if (!smids.length) continue
    for (const smid of smids) {
      try {
        const res = await fetch(`/api/messages/srv_${smid}`)
        if (!res.ok) continue
        const refMsg = await res.json()
        const info = getShareInfo(refMsg)
        if (info.title || info.cover) {
          sysRefCache[msg.msg_id] = info
          break
        }
      } catch {}
    }
  }
}

function openVideoById(itemId) {
  if (itemId) window.open(`https://www.douyin.com/video/${itemId}`, '_blank')
}

function tryParseJson(str) {
  if (!str || !str.startsWith('{')) return null
  try { return JSON.parse(str) } catch { return null }
}

// 判断是否为评论引用视频消息（aweType=700 + related_share_video）
function isVideoComment(msg) {
  const cj = getContentJson(msg)
  if (!cj) return false
  return cj.aweType === 700 && !!cj.related_share_video?.itemId
}

// 判断 msg_type=1 的消息是否实际上是分享卡片（JSON content 含 content_title）
function isJsonShare(msg) {
  if (msg.msg_type === 4) return false
  if (!msg.content || !msg.content.startsWith('{')) return false
  // 快速检查关键字段
  return msg.content.includes('content_title') || msg.content.includes('cover_url')
}

// 分享卡片信息提取
function getShareInfo(msg) {
  // 优先从 content_json (raw_data) 提取
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

  // aweType=10500: 引用视频评论 (comment 字段)
  // aweType=700: 引用视频评论 (text 字段)
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

function tryParseShareContent(content) {
  if (!content || !content.startsWith('{')) return null
  try {
    const obj = JSON.parse(content)
    if (obj.content_title || obj.cover_url) return obj
  } catch {}
  return null
}

function extractShareTitle(content) {
  if (!content) return ''
  // "分享[商品]: 商品名称" / "分享[视频]: 标题" / "[分享视频]标题"
  const m = content.match(/^(?:分享\[.+?\][:：]\s*|^\[分享视频\])(.+)/s)
  return m ? m[1].trim() : ''
}

// 图片 inline_pic base64 fallback
function getInlinePic(msg) {
  const cj = getContentJson(msg)
  if (cj?.inline_pic) {
    return 'data:image/webp;base64,' + cj.inline_pic.replace(/\r?\n/g, '')
  }
  return null
}

// 是否是视频消息（msg_type=3 但本地是 mp4 —— 实际视频文件已落地）
function isVideoMsg(msg) {
  return msg.media_local_path && /\.mp4$/i.test(msg.media_local_path)
}

// 是否是 JSON 视频消息（msg_type=5 新分类，或老数据 msg_type=1 带 cj.video.vid）
// 这类只有 poster 封面图，没有真正的视频文件（vid→URL 反查 TODO）
function isJsonVideo(msg) {
  if (msg.msg_type === 5) return true
  if (msg.msg_type !== 1) return false
  const cj = getContentJson(msg)
  return !!(cj && cj.video && cj.video.vid)
}

// 本地是否有 .mp4 视频文件可播（与 isVideoMsg 同一判定）
const hasLocalVideo = isVideoMsg

// 视频封面：优先 inline_pic（仅做缩略图）；本地路径若是 .mp4 则是视频本体，不当封面用
function getVideoPoster(msg) {
  // inline_pic 是 base64 WebP 缩略图，在 cj 里
  return getInlinePic(msg)
}

// 视频时长（秒）
function getVideoDuration(msg) {
  const cj = getContentJson(msg)
  const d = cj?.duration
  if (d === undefined || d === null) return ''
  const n = typeof d === 'string' ? parseFloat(d) : Number(d)
  if (!n || isNaN(n)) return ''
  return Math.round(n) + '″'
}

// 图片源：本地大图 > inline_pic 缩略图（视频走单独分支）
function getImageSrc(msg) {
  if (msg.media_local_path && !isVideoMsg(msg)) return '/media/' + msg.media_local_path
  return getInlinePic(msg)
}

// 右键消息体 → 全选其内容（让浏览器原生右键菜单的"复制"直接生效）
function selectMsgContent(e) {
  const body = e.currentTarget
  if (!body) return
  // 优先精确选中消息内容容器（避开 sender / time）
  const content = body.querySelector(
    ':scope > .msg-bubble, :scope > .msg-share-card, :scope > .msg-share-comment, :scope > .msg-media, :scope > .msg-voice'
  )
  const target = content || body
  const range = document.createRange()
  range.selectNodeContents(target)
  const sel = window.getSelection()
  sel.removeAllRanges()
  sel.addRange(range)
  // 不阻止默认事件 —— 浏览器原生菜单照常出现，"Copy" 即可
}

// 右键系统消息 → 全选
function selectSystemContent(e) {
  const el = e.currentTarget
  if (!el) return
  const range = document.createRange()
  range.selectNodeContents(el)
  const sel = window.getSelection()
  sel.removeAllRanges()
  sel.addRange(range)
}

// 表情源：本地 > CDN URL
function getEmojiSrc(msg) {
  if (msg.media_local_path) return '/media/' + msg.media_local_path
  return msg.media_url || null
}

// 撤回消息检测
function isRecalled(msg) {
  const cj = getContentJson(msg)
  if (cj?.is_recalled) return true
  if (!msg.raw_data) return false
  try {
    const raw = typeof msg.raw_data === 'string' ? JSON.parse(msg.raw_data) : msg.raw_data
    return !!raw.is_recalled
  } catch { return false }
}

// 语音消息检测
function isVoiceMsg(msg) {
  const cj = getContentJson(msg)
  if (cj?.resource_url?.url_list?.length) return true
  // 也检查 content 字段
  if (msg.content?.startsWith('{') && msg.content.includes('resource_url')) {
    try { const o = JSON.parse(msg.content); return !!o.resource_url?.url_list?.length } catch {}
  }
  return false
}

function getVoiceUrl(msg) {
  const cj = getContentJson(msg)
  // Guard JSON.parse: malformed content that merely starts with '{' must not
  // throw during render (matches getVoiceDuration below).
  const source = cj || (msg.content?.startsWith('{') ? (() => { try { return JSON.parse(msg.content) } catch { return null } })() : null)
  if (!source?.resource_url?.url_list?.length) return ''
  // 优先使用本地路径
  if (msg.media_local_path) return `/media/${msg.media_local_path}`
  return source.resource_url.url_list[0]
}

function getVoiceDuration(msg) {
  const cj = getContentJson(msg)
  const source = cj || (msg.content?.startsWith('{') ? (() => { try { return JSON.parse(msg.content) } catch { return null } })() : null)
  if (!source?.duration) return '?'
  return Math.round(source.duration / 1000)
}

// 回复/引用消息解析
function getRefMsg(msg) {
  if (!msg.ref_msg) return null
  try {
    const ref = typeof msg.ref_msg === 'string' ? JSON.parse(msg.ref_msg) : msg.ref_msg
    // 新格式（field 18）：有 content 和 nickname
    if (ref.content || ref.nickname) return ref
    // 旧格式：有 server_id 或 content_json
    if (ref.server_id && String(ref.server_id).length >= 15) return ref
    if (ref.content_json) return ref
  } catch {}
  return null
}

function getRefContent(ref) {
  if (!ref) return ''
  // 新格式（field 18）：直接使用 content 字段
  if (ref.content) return ref.content
  // 旧格式：从 refmsg_content 或 content_json 解析
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

function getRefNickname(ref) {
  if (!ref) return ''
  return ref.nickname || ''
}

// 高亮的消息 ID
const highlightMsgId = ref(null)

// Lightbox state
const lightboxSrc = ref(null)
function openLightbox(src) {
  if (!src) return
  lightboxSrc.value = src
}
function closeLightbox() { lightboxSrc.value = null }
function onLightboxKey(e) {
  if (e.key === 'Escape') closeLightbox()
}

async function jumpToRefMsg(ref) {
  if (!ref || !ref.server_id || !props.conversation) return
  const msgId = `srv_${ref.server_id}`
  // 锁定滚动，防止无限加载干扰
  scrollLocked = true
  try {
    // 先检查当前已加载的消息中是否有目标
    const existing = listRef.value?.querySelector(`[data-msgid="${msgId}"]`)
    if (existing) {
      existing.scrollIntoView({ block: 'center' })
      highlightMsgId.value = msgId
      setTimeout(() => { highlightMsgId.value = null }, 2000)
      return
    }
    // 需要加载目标消息附近的消息
    const res = await fetch(`/api/messages/${msgId}`)
    if (!res.ok) return
    const msg = await res.json()
    if (!msg.seq) return
    const targetSeq = Math.max(0, msg.seq - 50)
    messages.value = []
    cjCache.clear()
    loading.value = true
    const url = `/api/conversations/${props.conversation.conv_id}/messages?page_size=100&after_seq=${targetSeq}`
    const res2 = await fetch(url)
    const data = await res2.json()
    loading.value = false
    messages.value = data.items
    total.value = data.total
    hasMore.value = messages.value.length < data.total
    atLatest.value = false
    loadUserInfoForMessages(data.items)
    loadSysRefs(data.items)
    await nextTick()
    const el = listRef.value?.querySelector(`[data-msgid="${msgId}"]`)
    if (el) {
      el.scrollIntoView({ block: 'center' })
      highlightMsgId.value = msgId
      setTimeout(() => { highlightMsgId.value = null }, 2000)
    }
  } catch {} finally {
    setTimeout(() => { scrollLocked = false }, 300)
  }
}

// 打开分享视频
function openShare(msg) {
  const info = getShareInfo(msg)
  if (info.productUrl) {
    window.open(info.productUrl, '_blank')
  } else if (info.itemId) {
    window.open(`https://www.douyin.com/video/${info.itemId}`, '_blank')
  }
}

async function fetchSenders(convId) {
  try {
    const res = await fetch(`/api/conversations/${convId}/senders`)
    senders.value = await res.json()
    // 预加载发送者的用户信息
    for (const s of senders.value) {
      fetchUserInfo(s.sender_uid)
    }
  } catch {}
}

async function fetchMessages(convId, beforeSeq = null, afterSeq = null) {
  loading.value = true
  let url = `/api/conversations/${convId}/messages?page_size=100`
  if (beforeSeq !== null) url += `&before_seq=${beforeSeq}`
  if (afterSeq !== null) url += `&after_seq=${afterSeq}`
  let data
  try {
    const res = await fetch(url)
    if (!res.ok) throw new Error(`HTTP ${res.status}`)
    data = await res.json()
  } catch (e) {
    // Don't leave the spinner (and scroll lock) stuck forever on a failed load.
    loading.value = false
    scrollLocked = false
    console.error('加载消息失败', e)
    return
  }
  loading.value = false

  // 清理缓存
  cjCache.clear()

  scrollLocked = true
  if (beforeSeq === null && afterSeq === null) {
    // 初始加载（最新消息）
    messages.value = data.items
    atLatest.value = true
    await nextTick()
    if (listRef.value) listRef.value.scrollTop = listRef.value.scrollHeight
  } else if (afterSeq !== null && afterSeq === 0) {
    // 跳到最早消息
    messages.value = data.items
    atLatest.value = false
    await nextTick()
    if (listRef.value) listRef.value.scrollTop = 0
  } else if (afterSeq !== null) {
    // 向下加载更新的消息（从最早端向下加载更多）
    messages.value = [...messages.value, ...data.items]
    // 如果没有更多新消息了，说明已到达最新
    if (data.items.length === 0 || messages.value.length >= data.total) {
      atLatest.value = true
    }
  } else {
    // 加载更早的消息（向上加载更多）
    const list = listRef.value
    const prevHeight = list ? list.scrollHeight : 0
    messages.value = [...data.items, ...messages.value]
    await nextTick()
    if (list) list.scrollTop = list.scrollHeight - prevHeight
  }
  setTimeout(() => { scrollLocked = false }, 200)

  total.value = data.total
  hasMore.value = messages.value.length < data.total

  // 异步加载用户信息（头像、昵称）
  loadUserInfoForMessages(data.items)
  // 异步加载系统消息引用的视频
  loadSysRefs(data.items)
}

function loadMore() {
  if (props.conversation && messages.value.length > 0) {
    if (atLatest.value) {
      // 在最新端：向上加载更早的消息
      const minSeq = Math.min(...messages.value.map(m => m.seq))
      fetchMessages(props.conversation.conv_id, minSeq)
    } else {
      // 在最早端（跳转到顶部后）：向下加载更新的消息
      const maxSeq = Math.max(...messages.value.map(m => m.seq))
      fetchMessages(props.conversation.conv_id, null, maxSeq)
    }
  }
}

async function jumpToTop() {
  if (!props.conversation) return
  await fetchMessages(props.conversation.conv_id, null, 0)
}

async function jumpToBottom() {
  if (!props.conversation) return
  // 重新加载最新消息（无 beforeSeq / afterSeq）
  await fetchMessages(props.conversation.conv_id)
}

// 无限滚动：滚动到顶部或底部时自动加载更多
let scrollDebounce = null
function onListScroll() {
  if (scrollDebounce || scrollLocked) return
  const list = listRef.value
  if (!list || loading.value || !hasMore.value) return
  if (!props.conversation) return  // scroll event after the conversation was cleared
  const threshold = 100
  // 滚到顶部附近 → 加载更早消息
  if (list.scrollTop < threshold) {
    const minSeq = Math.min(...messages.value.map(m => m.seq))
    if (minSeq > 1) {
      scrollDebounce = true
      fetchMessages(props.conversation.conv_id, minSeq)
      setTimeout(() => { scrollDebounce = false }, 500)
    }
  }
  // 滚到底部附近 → 加载更新消息
  if (list.scrollHeight - list.scrollTop - list.clientHeight < threshold && !atLatest.value) {
    scrollDebounce = true
    const maxSeq = Math.max(...messages.value.map(m => m.seq))
    fetchMessages(props.conversation.conv_id, null, maxSeq)
    setTimeout(() => { scrollDebounce = false }, 500)
  }
}

onMounted(() => {
  // 延迟绑定，等 listRef 准备好
  const tryBind = () => {
    if (listRef.value) {
      listRef.value.addEventListener('scroll', onListScroll, { passive: true })
    } else {
      setTimeout(tryBind, 200)
    }
  }
  tryBind()
  window.addEventListener('keydown', onLightboxKey)
})
onUnmounted(() => {
  if (listRef.value) {
    listRef.value.removeEventListener('scroll', onListScroll)
  }
  window.removeEventListener('keydown', onLightboxKey)
})

function formatTime(ts) {
  if (!ts) return ''
  const d = new Date(ts * 1000)
  const now = new Date()
  const isToday = d.toDateString() === now.toDateString()
  const time = d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
  if (isToday) return time
  return d.toLocaleDateString('zh-CN', { month: 'short', day: 'numeric' }) + ' ' + time
}

// Highlight the active search term inside a message bubble.
function highlightText(text) {
  return _highlightText(text, props.searchHighlight)
}

function onImgError(e) {
  e.target.style.display = 'none'
}

watch(() => props.conversation, (conv) => {
  if (conv) {
    messages.value = []
    cjCache.clear()
    fetchSenders(conv.conv_id)
    // 如果有 jumpToSeq，由 jumpToSeq watcher 处理加载
    if (!props.jumpToSeq) {
      fetchMessages(conv.conv_id)
    }
  }
})

watch(() => props.jumpToSeq, async (seq) => {
  if (seq && props.conversation) {
    messages.value = []
    cjCache.clear()
    const targetSeq = Math.max(0, seq - 50)
    await fetchMessages(props.conversation.conv_id, null, targetSeq)
    await nextTick()
    // 精确滚动到目标消息并高亮
    const targetMsg = messages.value.find(m => m.seq === seq)
    if (targetMsg && listRef.value) {
      const el = listRef.value.querySelector(`[data-msgid="${targetMsg.msg_id}"]`)
      if (el) {
        el.scrollIntoView({ block: 'center' })
        highlightMsgId.value = targetMsg.msg_id
        setTimeout(() => { highlightMsgId.value = null }, 3000)
      }
    }
    emit('jumped')
  }
})
</script>

<style scoped>
.msg-panel {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  background: var(--bg-primary);
  position: relative;
}

.msg-empty {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  color: var(--text-muted);
  gap: 12px;
}
.msg-empty-icon {
  font-size: 48px;
}

.msg-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid var(--border-color);
  background: var(--bg-secondary);
  gap: 12px;
}
.msg-header h3 {
  font-size: 15px;
  font-weight: 600;
}
.msg-total {
  font-size: 12px;
  color: var(--text-muted);
}

.msg-pick-self {
  margin-left: auto;
  padding: 4px 12px;
  border: 1px solid var(--border-color);
  border-radius: 14px;
  background: var(--bg-tertiary);
  color: var(--text-primary);
  font-size: 12px;
  cursor: pointer;
}
.msg-pick-self:hover { border-color: var(--accent); }
.msg-pick-self.picked { border-color: var(--accent); color: var(--accent); }

/* UID 选择弹窗 */
.picker-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.5);
  z-index: 200;
  display: flex;
  align-items: center;
  justify-content: center;
}
.picker-dialog {
  background: var(--bg-secondary);
  border-radius: 12px;
  padding: 24px;
  min-width: 280px;
  box-shadow: 0 8px 30px rgba(0,0,0,0.4);
}
.picker-title {
  font-size: 16px;
  font-weight: 600;
  margin-bottom: 4px;
}
.picker-hint {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 16px;
}
.picker-option {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 14px;
  border: 1px solid var(--border-color);
  border-radius: 8px;
  margin-bottom: 8px;
  cursor: pointer;
  transition: border-color 0.15s;
}
.picker-option:hover { border-color: var(--accent); }
.picker-option.active { border-color: var(--accent); background: var(--bg-tertiary); }
.picker-uid { font-family: monospace; font-size: 13px; }
.picker-count { font-size: 12px; color: var(--text-muted); }
.picker-clear {
  width: 100%;
  margin-top: 8px;
  padding: 6px;
  border: none;
  background: none;
  color: var(--text-muted);
  font-size: 12px;
  cursor: pointer;
}
.picker-clear:hover { color: var(--accent); }

.msg-list {
  flex: 1;
  overflow-y: auto;
  padding: 16px 20px 32px;
}

.msg-loading, .msg-no-data {
  text-align: center;
  color: var(--text-muted);
  padding: 20px;
  font-size: 13px;
}

.msg-load-more {
  text-align: center;
  color: var(--accent);
  padding: 16px 10px;
  margin: 8px 0;
  cursor: pointer;
  font-size: 13px;
  flex-shrink: 0;
}
.msg-load-more:hover {
  color: var(--accent-hover);
}

.msg-jump-fab {
  position: fixed;
  right: 32px;
  z-index: 100;
  padding: 8px 18px;
  background: var(--accent);
  color: #fff;
  border: none;
  border-radius: 20px;
  font-size: 13px;
  cursor: pointer;
  box-shadow: 0 2px 10px rgba(0,0,0,0.35);
}
.msg-jump-fab:hover { opacity: 0.85; }
.msg-jump-top { bottom: 28px; }
.msg-jump-bottom { bottom: 28px; }

/* 系统消息 */
.msg-item.msg-system {
  max-width: 100%;
  justify-content: center;
}
.msg-system-block {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  max-width: 80%;
}
.msg-system-text {
  font-size: 12px;
  color: var(--text-secondary);
  background: color-mix(in srgb, var(--text-primary) 6%, transparent);
  padding: 4px 14px;
  border-radius: 999px;
  text-align: center;
}
.msg-system-ref {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 10px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  cursor: pointer;
  max-width: 280px;
  transition: border-color 0.15s;
}
.msg-system-ref:hover {
  border-color: var(--accent);
}
.msg-system-ref-cover {
  width: 36px;
  height: 36px;
  border-radius: 4px;
  object-fit: cover;
  flex-shrink: 0;
}
.msg-system-ref-title {
  font-size: 12px;
  color: var(--text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
  line-height: 1.3;
}

.msg-item {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
  max-width: 74%;
  transition: background 0.3s;
}
.msg-item.msg-highlight {
  background: color-mix(in srgb, var(--highlight) 22%, transparent);
  border-radius: var(--radius);
  animation: highlight-fade 2s ease-out;
}
@keyframes highlight-fade {
  0% { background: color-mix(in srgb, var(--highlight) 40%, transparent); }
  100% { background: transparent; }
}
.msg-item.msg-self {
  flex-direction: row-reverse;
  margin-left: auto;
}

.msg-avatar {
  width: 36px;
  height: 36px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  font-weight: 600;
  flex-shrink: 0;
  margin-top: 2px;
  overflow: hidden;
}
.msg-avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  border-radius: 50%;
}
.msg-avatar span {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
}

.msg-body {
  min-width: 0;
}

.msg-sender {
  font-size: 12px;
  color: var(--text-muted);
  margin-bottom: 3px;
}
.msg-item.msg-self .msg-sender {
  text-align: right;
}

.msg-bubble {
  background: var(--bg-message-other);
  padding: 9px 13px;
  border-radius: var(--bubble-radius);
  border-top-left-radius: 4px;
  font-size: 14px;
  line-height: 1.55;
  word-break: break-word;
  white-space: pre-wrap;
  box-shadow: var(--shadow-sm);
  border: 1px solid color-mix(in srgb, var(--text-primary) 5%, transparent);
}
.msg-item.msg-self .msg-bubble {
  background: var(--bg-message-self);
  color: #fff;
  border: none;
  border-top-left-radius: var(--bubble-radius);
  border-top-right-radius: 4px;
}

/* 分享卡片 */
.msg-share-card {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: var(--bg-message-other);
  border-radius: 10px;
  border-top-left-radius: 2px;
  padding: 10px 12px;
  cursor: pointer;
  transition: filter 0.15s;
  max-width: 320px;
  border-left: 3px solid var(--accent);
}
.msg-share-comment {
  font-size: 14px;
  line-height: 1.5;
  word-break: break-word;
}
.msg-share-comment-user {
  font-weight: 600;
  color: var(--accent);
}
.msg-share-comment-img {
  display: block;
  max-width: 180px;
  max-height: 180px;
  border-radius: 6px;
  margin-top: 6px;
  object-fit: contain;
}
.msg-share-card-inner {
  display: flex;
  gap: 10px;
}
.msg-share-card:hover {
  filter: brightness(1.1);
}
.msg-item.msg-self .msg-share-card {
  background: var(--bg-message-self);
  border-top-left-radius: 10px;
  border-top-right-radius: 2px;
}
.msg-share-card-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.msg-share-card-title {
  font-size: 13px;
  line-height: 1.4;
  display: -webkit-box;
  -webkit-line-clamp: 3;
  -webkit-box-orient: vertical;
  overflow: hidden;
  word-break: break-word;
}
.msg-share-card-author {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: auto;
}
.msg-share-card-cover {
  width: 60px;
  height: 60px;
  border-radius: 6px;
  object-fit: cover;
  flex-shrink: 0;
  align-self: center;
}
.msg-share-card-ref {
  background: var(--bg-tertiary);
  border-radius: 6px;
  padding: 6px 10px;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--text-secondary);
}
.msg-share-card-ref-icon {
  opacity: 0.6;
}

/* 引用/回复消息 */
.msg-ref-quote {
  padding: 6px 10px;
  margin-bottom: 4px;
  background: var(--bg-tertiary);
  border-left: 2px solid var(--text-muted);
  border-radius: 4px;
  font-size: 12px;
  color: var(--text-muted);
  max-width: 300px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
  transition: background 0.2s;
}
.msg-ref-quote:hover {
  background: var(--border-color);
}
.msg-ref-name {
  font-weight: 600;
  color: var(--text-secondary);
}
.msg-ref-content {
  opacity: 0.85;
}

/* 语音消息 */
.msg-voice {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 0;
}
.msg-voice audio {
  height: 36px;
  max-width: 260px;
}
.msg-voice-dur {
  font-size: 12px;
  color: var(--text-muted);
  white-space: nowrap;
}

/* 表情/图片 */
.msg-media img {
  max-width: 240px;
  max-height: 240px;
  border-radius: 8px;
  cursor: pointer;
}
.msg-media video {
  max-width: 280px;
  max-height: 360px;
  border-radius: 8px;
  background: #000;
}
.msg-inline-pic {
  opacity: 0.85;
  filter: blur(0.5px);
}
.msg-media-missing {
  font-size: 12px;
  color: var(--text-muted);
  padding: 8px 12px;
  background: var(--bg-tertiary);
  border-radius: 8px;
}
/* 视频封面 + 时长 overlay */
.msg-video-poster {
  position: relative;
  display: inline-block;
  cursor: pointer;
}
.msg-video-player {
  max-width: 280px;
  max-height: 360px;
  border-radius: 8px;
  background: #000;
}
.msg-video-overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  pointer-events: none;
  border-radius: 8px;
  background: linear-gradient(rgba(0,0,0,0) 60%, rgba(0,0,0,0.5));
}
.msg-video-play {
  font-size: 36px;
  color: rgba(255,255,255,0.92);
  text-shadow: 0 2px 8px rgba(0,0,0,0.6);
  line-height: 1;
}
.msg-video-dur {
  position: absolute;
  right: 8px;
  bottom: 6px;
  background: rgba(0,0,0,0.55);
  color: #fff;
  font-size: 12px;
  padding: 2px 6px;
  border-radius: 3px;
}

/* Lightbox */
.lightbox-overlay {
  position: fixed; inset: 0; z-index: 9999;
  background: rgba(0, 0, 0, 0.85);
  display: flex; align-items: center; justify-content: center;
  cursor: zoom-out;
  animation: lightboxIn 0.15s ease-out;
}
@keyframes lightboxIn { from { opacity: 0; } to { opacity: 1; } }
.lightbox-img {
  max-width: 92vw; max-height: 92vh;
  object-fit: contain;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
  border-radius: 4px;
  cursor: zoom-out;
}
.lightbox-close {
  position: absolute; top: 20px; right: 28px;
  width: 40px; height: 40px;
  background: rgba(255, 255, 255, 0.1);
  border: none; border-radius: 50%;
  color: #fff; font-size: 28px; line-height: 1;
  cursor: pointer;
  transition: background 0.15s;
}
.lightbox-close:hover { background: rgba(255, 255, 255, 0.2); }

.msg-time {
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 3px;
}
.msg-item.msg-self .msg-time {
  text-align: right;
}
.msg-recalled-tag {
  font-size: 10px;
  color: #e5534b;
  background: rgba(229, 83, 75, 0.1);
  padding: 1px 5px;
  border-radius: 3px;
  margin-right: 4px;
}
</style>
