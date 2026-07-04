import { describe, it, expect } from 'vitest'
import {
  getContentJson, isJsonShare, getShareInfo, renderSystemMsg, shouldShow,
  isJsonSticker, isJsonSystemMsg, isVoiceMsg, getVoiceDuration, getVoiceUrl,
  isVideoMsg, isJsonVideo, getVideoDuration, getInlinePic, getImageSrc, getEmojiSrc,
  getRefMsg, getRefContent, getRefNickname, extractServerMsgIds, isRecalled,
  getWatchTogether,
} from './douyinMessage.js'

// Build a message row. content_json is double-encoded inside raw_data, like the DB.
let _id = 0
function msg(fields = {}) {
  return { msg_id: `t${_id++}`, msg_type: 1, content: '', media_local_path: null,
           media_url: null, raw_data: null, ref_msg: null, timestamp: 0, sender_uid: 'u', ...fields }
}
function withCj(cj, fields = {}) {
  return msg({ raw_data: JSON.stringify({ content_json: JSON.stringify(cj) }), ...fields })
}

describe('getContentJson', () => {
  it('parses double-encoded content_json', () => {
    expect(getContentJson(withCj({ a: 1 }))).toEqual({ a: 1 })
  })
  it('returns null without raw_data', () => {
    expect(getContentJson(msg())).toBeNull()
  })
})

describe('share detection + extraction', () => {
  it('isJsonShare true for content_title JSON, false for msg_type 4', () => {
    expect(isJsonShare(msg({ content: '{"content_title":"x"}' }))).toBe(true)
    expect(isJsonShare(msg({ content: '{"content_title":"x"}', msg_type: 4 }))).toBe(false)
    expect(isJsonShare(msg({ content: 'plain text' }))).toBe(false)
  })
  it('getShareInfo pulls video-share fields', () => {
    const info = getShareInfo(withCj({ itemId: '42', content_title: 'T', content_name: 'A',
                                       cover_url: { url_list: ['http://c'] } }))
    expect(info.title).toBe('T')
    expect(info.author).toBe('A')
    expect(info.itemId).toBe('42')
    expect(info.cover).toBe('http://c')
  })
  it('getShareInfo resolves a product card via im_dynamic_patch', () => {
    const patch = { raw_data: JSON.stringify({
      content_top: { content: '商品名' },
      whole_card: { action_info: [{ params: { schema: 'x?commodity_id=999&y=1' } }] },
    }) }
    const info = getShareInfo(withCj({ im_dynamic_patch: patch }))
    expect(info.title).toBe('商品名')
    expect(info.productUrl).toBe('https://www.douyin.com/product/999')
  })
})

describe('system messages', () => {
  it('renders a tips template', () => {
    const m = withCj({ tips: '{{1}}赞了你的 {{2}}',
                       template: [{ key: 1, name: '对方' }, { key: 2, name: '视频' }] }, { msg_type: 0 })
    expect(renderSystemMsg(m)).toBe('对方赞了你的 视频')
  })
  it('shouldShow hides empty system, keeps text', () => {
    expect(shouldShow(msg({ msg_type: 0, content: '{}' }))).toBe(false)
    expect(shouldShow(msg({ msg_type: 1, content: 'hi' }))).toBe(true)
  })
  it('isJsonSystemMsg needs both tips and aweType', () => {
    expect(isJsonSystemMsg(msg({ content: '{"tips":"x","aweType":1}' }))).toBe(true)
    expect(isJsonSystemMsg(msg({ content: '{"tips":"x"}' }))).toBe(false)
  })
})

describe('sticker / voice / video detection', () => {
  it('isJsonSticker via content or content_json', () => {
    expect(isJsonSticker(msg({ content: '{"stickers":[]}' }))).toBe(true)
    expect(isJsonSticker(withCj({ joker_stickers: [{}] }, { content: '{"x":1}' }))).toBe(true)
  })
  it('voice: detected by resource_url, duration in ms', () => {
    const v = withCj({ resource_url: { url_list: ['http://v'] }, duration: 4200 }, { msg_type: 0 })
    expect(isVoiceMsg(v)).toBe(true)
    expect(getVoiceDuration(v)).toBe(4)
    expect(getVoiceUrl(v)).toBe('http://v')
  })
  it('video: msg_type 5 or cj.video.vid; duration in seconds with ″', () => {
    expect(isJsonVideo(msg({ msg_type: 5 }))).toBe(true)
    expect(isJsonVideo(withCj({ video: { vid: 'v1' } }))).toBe(true)
    expect(getVideoDuration(withCj({ duration: 12 }))).toBe('12″')
    expect(isVideoMsg(msg({ media_local_path: 'videos/x.mp4' }))).toBeTruthy()
    expect(isVideoMsg(msg({ media_local_path: 'images/x.jpg' }))).toBeFalsy()
  })
})

describe('media src helpers', () => {
  it('getInlinePic strips embedded newlines', () => {
    expect(getInlinePic(withCj({ inline_pic: 'AA\nBB\r\nCC' }))).toBe('data:image/webp;base64,AABBCC')
  })
  it('getImageSrc prefers local non-mp4, getEmojiSrc prefers local', () => {
    expect(getImageSrc(msg({ media_local_path: 'images/a.jpg' }))).toBe('/media/images/a.jpg')
    expect(getEmojiSrc(msg({ media_local_path: 'emoji/e.webp' }))).toBe('/media/emoji/e.webp')
    expect(getEmojiSrc(msg({ media_url: 'http://cdn/e' }))).toBe('http://cdn/e')
  })
})

describe('reply/quote', () => {
  it('new format uses content/nickname directly', () => {
    const m = msg({ ref_msg: JSON.stringify({ content: '原文', nickname: '小明' }) })
    const ref = getRefMsg(m)
    expect(getRefContent(ref)).toBe('原文')
    expect(getRefNickname(ref)).toBe('小明')
  })
  it('old format maps emoji/share content_json', () => {
    expect(getRefContent({ content_json: JSON.stringify({ aweType: 501 }) })).toBe('[表情]')
    expect(getRefContent({ content_json: JSON.stringify({ content_title: 'T' }) })).toBe('[分享] T')
  })
})

describe('watch-together invite (aweType 9000)', () => {
  const cj = { aweType: 9000, title: '邀你一起看视频', sub_title: '加入和我一起看',
               cover_url: { url_list: ['http://c/card.png'] }, room_id: 123 }
  it('getWatchTogether pulls title/subtitle/cover, null otherwise', () => {
    const wt = getWatchTogether(withCj(cj, { msg_type: 0 }))
    expect(wt).toEqual({ title: '邀你一起看视频', subtitle: '加入和我一起看', cover: 'http://c/card.png' })
    expect(getWatchTogether(msg({ msg_type: 0, content: 'hi' }))).toBeNull()
  })
  it('is shown (not hidden as an empty system message)', () => {
    expect(shouldShow(withCj(cj, { msg_type: 0 }))).toBe(true)
  })
  it('also detected when the JSON is only in content', () => {
    expect(getWatchTogether(msg({ msg_type: 0, content: JSON.stringify(cj) }))).not.toBeNull()
  })
})

describe('renderSystemMsg never leaks raw JSON', () => {
  it('unrecognized JSON card -> empty string, not the raw JSON', () => {
    expect(renderSystemMsg(msg({ msg_type: 0, content: '{"aweType":9000,"title":"x"}' }))).toBe('')
  })
  it('plain text system content still shows', () => {
    expect(renderSystemMsg(msg({ msg_type: 0, content: '你已添加对方为好友' }))).toBe('你已添加对方为好友')
  })
})

describe('misc', () => {
  it('extractServerMsgIds finds 15+ digit ids in raw string', () => {
    const m = msg({ raw_data: 'x server_message_id":123456789012345 y' })
    expect(extractServerMsgIds(m)).toEqual(['123456789012345'])
  })
  it('isRecalled reads is_recalled', () => {
    expect(isRecalled(withCj({ is_recalled: true }))).toBe(true)
    expect(isRecalled(msg({ content: 'hi' }))).toBe(false)
  })
})
