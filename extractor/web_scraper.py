#!/usr/bin/env python3
"""Extract Douyin chat messages via web version using Playwright + DOM scraping."""
import asyncio
import json
import hashlib
import os
import re
import random
import time
from datetime import datetime, timedelta

from playwright.async_api import async_playwright

from extractor.models import (
    init_db, get_db, upsert_user, upsert_conversation, update_conversation_stats,
)

CHAT_URL = "https://www.douyin.com/chat?isPopup=1"
USER_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "browser_profile")

# ── DOM Selectors (from discovery) ──────────────────────────────
# Conversation list
SEL_CONV_LIST = 'div[class*="conversationConversationListwrapper"]'
SEL_CONV_ITEM = 'div[class*="conversationConversationItemwrapper"]'
SEL_CONV_TITLE = 'div[class*="conversationConversationItemtitle"]'
SEL_CONV_TIME = 'div[class*="ConversationItemTagNextToTitletimeStr"]'
SEL_CONV_PREVIEW = 'pre[class*="ConversationItemHinttextBox"]'

# Message area
SEL_MSG_LIST = 'div[class*="messageMessageListlist"]'
SEL_MSG_BOX = 'div[class*="messageMessageBoxmessageBox"]'
SEL_MSG_CONTENT_BOX = 'div[class*="messageMessageBoxcontentBox"]'
SEL_MSG_IS_SELF = 'messageMessageBoxisFromMe'  # class substring
SEL_MSG_TEXT = 'span[class*="TextMessageTextpureText"]'
SEL_MSG_TIME = 'div[class*="MessageBoxTimetimeLayout"]'
SEL_MSG_SHARE = 'div[class*="MessageItemShareAwemecontainer"]'
SEL_MSG_EMOJI = 'img[class*="MessageItemEmojiimage"]'
SEL_MSG_AVATAR = 'img[class*="avatar"]'

# ── 中文星期映射 ────────────────────────────────────────────────
WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}


class WebChatScraper:
    def __init__(self, discovery_mode=False, name_filter=None, incremental=False):
        self.discovery_mode = discovery_mode
        self.name_filter = name_filter
        self.incremental = incremental
        self.pw = None
        self.context = None
        self.page = None
        self._db_conn = None  # 持久数据库连接
        self._last_known_timestamp = 0  # 跨批次时间戳继承

    async def launch(self):
        os.makedirs(USER_DATA_DIR, exist_ok=True)
        init_db()
        self._db_conn = get_db()

        self.pw = await async_playwright().start()
        self.context = await self.pw.chromium.launch_persistent_context(
            USER_DATA_DIR,
            headless=os.environ.get("HEADLESS", "false").lower() == "true",
            viewport={"width": 1400, "height": 900},
            locale="zh-CN",
            args=["--disable-blink-features=AutomationControlled"],
        )
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)
        self.page = self.context.pages[0] if self.context.pages else await self.context.new_page()
        print("[+] 浏览器已启动")

    async def wait_for_login(self):
        await self.page.goto("https://www.douyin.com/", wait_until="domcontentloaded")
        print("[*] 正在检测登录状态...")

        for attempt in range(180):
            # Use Playwright cookie API to check HttpOnly cookies too
            cookies = await self.context.cookies("https://www.douyin.com")
            cookie_names = {c["name"] for c in cookies}
            logged_in = "sessionid" in cookie_names
            if logged_in:
                print("[+] 已检测到登录状态")
                return True
            if attempt == 0:
                print("[*] 未检测到登录，请在浏览器中扫码登录... (最多 3 分钟)")
            await asyncio.sleep(1)

        print("[-] 登录超时")
        return False

    async def navigate_to_chat(self):
        print("[*] 正在导航至私信页面...")

        for attempt in range(3):
            await self.page.goto(CHAT_URL, wait_until="domcontentloaded")
            try:
                await self.page.wait_for_selector(SEL_CONV_ITEM, timeout=20000)
                print(f"[+] 当前页面: {self.page.url}")
                return
            except Exception:
                if attempt < 2:
                    print(f"[!] 等待会话列表超时，第 {attempt+1} 次重试...")
                    await asyncio.sleep(3)
                else:
                    print("[!] 等待会话列表超时（已重试 3 次），页面可能未完全加载")

        await asyncio.sleep(1)
        print(f"[+] 当前页面: {self.page.url}")

    # ── Time Parsing ──────────────────────────────────────────────

    @staticmethod
    def _parse_time_label(label: str) -> int:
        """将 DOM 时间标签转为 Unix 秒时间戳。"""
        if not label or not label.strip():
            return 0

        label = label.strip()
        now = datetime.now()

        # "X分钟前"
        m = re.match(r"(\d+)\s*分钟前", label)
        if m:
            return int((now - timedelta(minutes=int(m.group(1)))).timestamp())

        # "X小时前"
        m = re.match(r"(\d+)\s*小时前", label)
        if m:
            return int((now - timedelta(hours=int(m.group(1)))).timestamp())

        # "刚刚"
        if label == "刚刚":
            return int(now.timestamp())

        # "昨天 HH:MM"
        m = re.match(r"昨天\s*(\d{1,2}):(\d{2})", label)
        if m:
            yesterday = now - timedelta(days=1)
            dt = yesterday.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return int(dt.timestamp())

        # "前天 HH:MM"
        m = re.match(r"前天\s*(\d{1,2}):(\d{2})", label)
        if m:
            day = now - timedelta(days=2)
            dt = day.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return int(dt.timestamp())

        # "星期X HH:MM"
        m = re.match(r"星期([一二三四五六日天])\s*(\d{1,2}):(\d{2})", label)
        if m:
            target_wd = WEEKDAY_MAP.get(m.group(1), 0)
            current_wd = now.weekday()
            days_back = (current_wd - target_wd) % 7
            if days_back == 0:
                days_back = 7  # 同一天指上周
            day = now - timedelta(days=days_back)
            dt = day.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
            return int(dt.timestamp())

        # "YYYY/MM/DD HH:MM" or "YYYY/MM/DD"
        m = re.match(r"(\d{4})/(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?", label)
        if m:
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
            hour = int(m.group(4)) if m.group(4) else 0
            minute = int(m.group(5)) if m.group(5) else 0
            try:
                dt = datetime(year, month, day, hour, minute)
                return int(dt.timestamp())
            except ValueError:
                pass

        # "MM/DD HH:MM" or "MM/DD"
        m = re.match(r"(\d{1,2})/(\d{1,2})(?:\s+(\d{1,2}):(\d{2}))?$", label)
        if m:
            month, day = int(m.group(1)), int(m.group(2))
            hour = int(m.group(3)) if m.group(3) else 0
            minute = int(m.group(4)) if m.group(4) else 0
            year = now.year
            try:
                dt = datetime(year, month, day, hour, minute)
                if dt > now:
                    dt = dt.replace(year=year - 1)
                return int(dt.timestamp())
            except ValueError:
                pass

        # "HH:MM" (今天)
        m = re.match(r"^(\d{1,2}):(\d{2})$", label)
        if m:
            dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            return int(dt.timestamp())

        # 无法解析
        return 0

    # ── Discovery ──────────────────────────────────────────────────

    async def run_discovery(self, duration=60):
        print(f"\n{'='*60}")
        print(f"  发现模式 — 分析 DOM 结构 ({duration}s)")
        print(f"{'='*60}\n")

        await self.navigate_to_chat()
        await asyncio.sleep(2)

        dom_info = await self._dump_dom_structure()
        debug_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "debug")
        os.makedirs(debug_dir, exist_ok=True)
        filepath = os.path.join(debug_dir, f"dom_structure_{int(time.time()*1000)}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(dom_info, f, ensure_ascii=False, indent=2)

        print(f"\n[*] 监听 {duration} 秒，请在浏览器中操作...")
        for i in range(duration):
            await asyncio.sleep(1)
            if i > 0 and i % 15 == 0:
                print(f"  [{i}/{duration}s]")
                await self._dump_dom_structure()

    async def _dump_dom_structure(self):
        dom_info = await self.page.evaluate("""() => {
            const result = { title: document.title, url: location.href, im_elements: {}, conv_containers: [], msg_containers: [] };
            document.querySelectorAll('*').forEach(el => {
                const cls = typeof el.className === 'string' ? el.className : '';
                const lower = cls.toLowerCase();
                if (lower.match(/session|conversation|chat|message|im-|inbox|msg|bubble/)) {
                    const key = el.tagName.toLowerCase() + '.' + cls.split(' ')[0]?.substring(0, 40);
                    if (!result.im_elements[key]) result.im_elements[key] = { count: 0, sample_text: '', class: cls, children: 0 };
                    result.im_elements[key].count++;
                    if (!result.im_elements[key].sample_text) result.im_elements[key].sample_text = el.textContent?.trim().substring(0, 80) || '';
                    result.im_elements[key].children = Math.max(result.im_elements[key].children, el.children.length);
                }
            });
            return result;
        }""")

        print(f"  [DOM] IM 元素类型: {len(dom_info.get('im_elements', {}))}")
        for key, info in sorted(dom_info.get("im_elements", {}).items()):
            if info["count"] >= 2:
                print(f"    {key} x{info['count']}  text: {info['sample_text'][:50]}")
        return dom_info

    # ── Extraction ─────────────────────────────────────────────────

    async def extract_all(self):
        await self.navigate_to_chat()
        await asyncio.sleep(2)

        print("[*] 正在加载会话列表...")
        conversations = await self._load_all_conversations()
        print(f"[+] 共发现 {len(conversations)} 个会话")

        if not conversations:
            print("[-] 未找到会话")
            # Save debug screenshot
            debug_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "debug_no_conv.png")
            try:
                await self.page.screenshot(path=debug_path)
                print(f"[*] 调试截图已保存: {debug_path}")
            except Exception:
                pass
            return

        if self.name_filter:
            # Support comma-separated multiple filters
            filter_parts = [f.strip() for f in self.name_filter.split(",") if f.strip()]
            filtered = [c for c in conversations
                        if any(fp in c.get("nickname", "") or fp in c["name"] for fp in filter_parts)]
            print(f"[*] 过滤后: {len(filtered)} 个会话匹配 \"{self.name_filter}\"")
            if not filtered:
                print(f"[-] 没有匹配的会话。全部会话名称:")
                for c in conversations:
                    print(f"    - {c.get('nickname', '')} ({c['name']})")
                return
            conversations = filtered

        for i, conv in enumerate(conversations):
            display_name = conv.get("nickname") or conv["name"]
            print(f"\n[{i+1}/{len(conversations)}] {display_name} (最后活跃: {conv['time']})")
            try:
                await self._extract_conversation(conv["index"], conv)
            except Exception as e:
                print(f"  [!] 错误: {e}")
                import traceback
                traceback.print_exc()
            await asyncio.sleep(0.5 + random.random())

        conn = self._db_conn
        stats = {
            "conversations": conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0],
            "messages": conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0],
            "users": conn.execute("SELECT COUNT(*) FROM users").fetchone()[0],
        }

        print(f"\n{'='*60}")
        print(f"  提取完成!")
        print(f"  会话: {stats['conversations']}")
        print(f"  消息: {stats['messages']}")
        print(f"  用户: {stats['users']}")
        print(f"{'='*60}")

    async def _load_all_conversations(self):
        """Scroll conversation list and collect all conversation items."""
        all_convs = []
        prev_count = 0

        for scroll_round in range(20):
            convs = await self.page.evaluate(f"""() => {{
                const items = document.querySelectorAll('{SEL_CONV_ITEM}');
                return Array.from(items).map((el, idx) => {{
                    const titleEl = el.querySelector('{SEL_CONV_TITLE}');
                    const timeEl = el.querySelector('{SEL_CONV_TIME}');
                    const previewEl = el.querySelector('{SEL_CONV_PREVIEW}');
                    // 提取纯昵称：内部嵌套的 title div 只含昵称
                    let nickname = '';
                    if (titleEl) {{
                        const innerTitle = titleEl.querySelector('div[class*="conversationConversationItemtitle"]');
                        nickname = (innerTitle && innerTitle !== titleEl)
                            ? innerTitle.textContent.trim()
                            : titleEl.childNodes[0]?.textContent?.trim() || '';
                    }}
                    return {{
                        name: titleEl ? titleEl.textContent.trim() : '',
                        nickname: nickname,
                        time: timeEl ? timeEl.textContent.trim() : '',
                        preview: previewEl ? previewEl.textContent.trim() : '',
                        index: idx,
                    }};
                }});
            }}""")

            all_convs = convs
            current_count = len(convs)

            if current_count > prev_count:
                print(f"  已加载 {current_count} 个会话...")
                prev_count = current_count
            elif scroll_round > 2:
                break

            await self.page.evaluate(f"""() => {{
                const list = document.querySelector('{SEL_CONV_LIST}');
                if (list) {{
                    const scrollable = list.querySelector('[style*="overflow"]') || list;
                    scrollable.scrollTop += 500;
                }}
            }}""")
            await asyncio.sleep(1)

        return all_convs

    async def _download_voice_files(self, messages):
        """下载语音消息的音频文件到本地"""
        voice_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media", "voice")
        os.makedirs(voice_dir, exist_ok=True)

        voice_msgs = []
        for m in messages:
            if m.get("msg_type") != "other":
                continue
            cj_str = m.get("content_json", "")
            if not cj_str or "resource_url" not in cj_str:
                continue
            try:
                cj = json.loads(cj_str)
                if cj.get("resource_url") and cj.get("duration"):
                    urls = cj["resource_url"].get("url_list", [])
                    if urls:
                        voice_msgs.append((m, urls[0], cj.get("duration", 0)))
            except (json.JSONDecodeError, KeyError):
                continue

        if not voice_msgs:
            return

        # 批量下载（通过浏览器 fetch 以携带 cookie）
        for m, url, duration in voice_msgs:
            server_id = m.get("server_id", "unknown")
            filename = f"{server_id}.mpeg"
            local_path = os.path.join(voice_dir, filename)
            rel_path = f"voice/{filename}"

            if os.path.exists(local_path):
                m["local_path"] = rel_path
                continue

            try:
                import urllib.request
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = resp.read()
                if len(data) > 100:
                    with open(local_path, "wb") as f:
                        f.write(data)
                    m["local_path"] = rel_path
                    dur_sec = round(duration / 1000)
                    print(f"  [voice] 已下载语音 {dur_sec}s: {filename}")
                else:
                    print(f"  [voice] 下载失败（空响应 {len(data)}B）: {server_id}")
            except Exception as e:
                print(f"  [voice] 下载失败: {server_id}: {e}")

    async def _extract_and_save_user_info(self, conv_id):
        """从 userInfoStore 提取用户信息（昵称、头像、unique_id），下载头像到本地。"""
        users = await self.page.evaluate("""() => {
            const result = [];
            try {
                const uis = window.userInfoStore;
                if (!uis) return result;

                // 当前登录用户
                const me = uis.curLoginUserInfo;
                if (me) {
                    result.push({
                        uid: String(me.uid || ''),
                        nickname: me.nickname || '',
                        unique_id: me.uniqueId || '',
                        avatar_url: me.avatarUrl || me.avatar300Url || '',
                    });
                }

                // usersInfoMap (MobX observable)
                const uim = uis.usersInfoMap;
                if (uim && uim.data_) {
                    for (const [k, v] of uim.data_.entries()) {
                        const u = v.value_ || v;
                        if (!u || !u.nickname) continue;
                        let avatarUrl = '';
                        if (u.avatar_thumb && u.avatar_thumb.url_list && u.avatar_thumb.url_list.length > 0) {
                            avatarUrl = u.avatar_thumb.url_list[0];
                        }
                        result.push({
                            uid: String(u.uid || k),
                            nickname: u.nickname || '',
                            unique_id: u.unique_id || '',
                            avatar_url: avatarUrl,
                        });
                    }
                }
            } catch(e) {}
            return result;
        }""")

        if not users:
            print(f"  [*] 未能从 userInfoStore 获取用户信息")
            return

        avatar_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "media", "avatars")
        os.makedirs(avatar_dir, exist_ok=True)

        conn = self._db_conn
        for u in users:
            uid = u.get("uid", "")
            if not uid:
                continue

            nickname = u.get("nickname", "")
            unique_id = u.get("unique_id", "")
            avatar_url = u.get("avatar_url", "")

            # 下载头像到本地
            local_avatar = None
            if avatar_url:
                ext = "jpg"
                if ".webp" in avatar_url:
                    ext = "webp"
                elif ".png" in avatar_url:
                    ext = "png"
                local_path = os.path.join(avatar_dir, f"{uid}.{ext}")
                if not os.path.exists(local_path):
                    try:
                        resp = await self.page.evaluate("""async (url) => {
                            try {
                                const r = await fetch(url, {credentials: 'include'});
                                if (!r.ok) return null;
                                const buf = await r.arrayBuffer();
                                return Array.from(new Uint8Array(buf));
                            } catch { return null; }
                        }""", avatar_url)
                        if resp and len(resp) > 100:
                            with open(local_path, "wb") as f:
                                f.write(bytes(resp))
                            local_avatar = f"avatars/{uid}.{ext}"
                            print(f"  [*] 已保存头像: {nickname} ({uid})")
                    except Exception as e:
                        print(f"  [!] 下载头像失败 {nickname}: {e}")
                else:
                    local_avatar = f"avatars/{uid}.{ext}"

            upsert_user(conn, uid, nickname=nickname,
                        avatar_url=local_avatar or avatar_url,
                        unique_id=unique_id)

        # 更新会话的 participant_uids
        uids = [u.get("uid") for u in users if u.get("uid")]
        if uids and conv_id:
            conn.execute(
                "UPDATE conversations SET participant_uids = ? WHERE conv_id = ?",
                (json.dumps(uids), conv_id),
            )

        conn.commit()
        print(f"  [*] 已保存 {len(users)} 个用户信息")

    async def _extract_conversation(self, conv_index, conv_info):
        """Click a conversation and extract all its messages."""
        conv_name = conv_info["name"]
        # 优先使用纯昵称（不含火花天数和时间）
        clean_name = conv_info.get("nickname") or conv_name
        # Will try to get real conversation ID from fiber data after clicking
        conv_id = hashlib.md5(conv_name.encode()).hexdigest()[:16]

        clicked = False
        conv_items = await self.page.query_selector_all(SEL_CONV_ITEM)
        for item in conv_items:
            title_el = await item.query_selector(SEL_CONV_TITLE)
            if title_el:
                title_text = (await title_el.inner_text()).strip()
                if clean_name in title_text or title_text in conv_name:
                    await item.click()
                    clicked = True
                    print(f"  [*] 已点击会话: {title_text}")
                    break

        if not clicked:
            print(f"  [!] 无法找到会话「{clean_name}」，跳过")
            return

        await asyncio.sleep(2)

        active_name = await self.page.evaluate(f"""() => {{
            const active = document.querySelector('{SEL_CONV_ITEM}[class*="curConversation"]');
            if (!active) return '';
            const title = active.querySelector('{SEL_CONV_TITLE}');
            return title ? title.textContent.trim() : '';
        }}""")
        print(f"  [*] 当前活跃会话: {active_name or '(未检测到)'}")

        # Try to get real conversation ID from IM SDK
        real_conv_id = await self.page.evaluate("""() => {
            const cs = window.conversationStore;
            return cs && cs.curConversationId ? String(cs.curConversationId) : null;
        }""")
        if real_conv_id:
            conv_id = real_conv_id
            print(f"  [*] 真实会话ID: {conv_id}")

        upsert_conversation(self._db_conn, conv_id, name=clean_name)
        self._db_conn.commit()

        # 提取用户信息（昵称、头像、unique_id）
        await self._extract_and_save_user_info(conv_id)

        mode_str = "增量" if self.incremental else "全量"
        print(f"  [*] 开始{mode_str}导出 (纯API模式)...")

        # ── 纯 API 模式：清除 SDK 缓存 → 重新加载 → 捕获 cursor → API 直取全部消息 ──

        # 1. 清除 SDK 本地缓存（localStorage/sessionStorage/IndexedDB，保留 cookies 以维持登录）
        print(f"  [*] 清除 SDK 本地缓存...")
        await self._clear_sdk_cache()

        # 2. 安装请求拦截器（在重新加载之前，确保捕获 SDK 的第一个 API 请求）
        self._captured_api_cursor = None
        cursor_captured_event = asyncio.Event()

        async def capture_api_request(request):
            if "get_by_conversation" in request.url and request.method == "POST":
                try:
                    body = request.post_data_buffer
                    if body:
                        result = await self.page.evaluate("""(bytes) => {
                            function dv(buf, pos) {
                                let r = 0n, s = 0n;
                                while (pos < buf.length) {
                                    const b = buf[pos++]; r |= BigInt(b & 0x7F) << s;
                                    if (!(b & 0x80)) break; s += 7n;
                                }
                                return [r, pos];
                            }
                            function ef(buf, tf) {
                                let pos = 0;
                                while (pos < buf.length) {
                                    let tag; [tag, pos] = dv(buf, pos);
                                    const fn = Number(tag >> 3n), wt = Number(tag & 7n);
                                    if (wt === 0) { let v; [v, pos] = dv(buf, pos); }
                                    else if (wt === 2) { let len; [len, pos] = dv(buf, pos); len = Number(len); if (fn === tf) return buf.slice(pos, pos + len); pos += len; }
                                    else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                                }
                                return null;
                            }
                            // 提取字符串字段 (wire type 2)
                            function efStr(buf, tf) {
                                let pos = 0;
                                while (pos < buf.length) {
                                    let tag; [tag, pos] = dv(buf, pos);
                                    const fn = Number(tag >> 3n), wt = Number(tag & 7n);
                                    if (wt === 0) { let v; [v, pos] = dv(buf, pos); }
                                    else if (wt === 2) {
                                        let len; [len, pos] = dv(buf, pos); len = Number(len);
                                        if (fn === tf) return new TextDecoder().decode(buf.slice(pos, pos + len));
                                        pos += len;
                                    }
                                    else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                                }
                                return null;
                            }
                            const data = new Uint8Array(bytes);
                            const f8 = ef(data, 8);
                            if (!f8) return null;
                            const f301 = ef(f8, 301);
                            if (!f301) return null;
                            // 提取 conv_id (field 1, string) 和 cursor (field 3, varint)
                            const reqConvId = efStr(f301, 1);
                            let cursor = null;
                            let pos = 0;
                            while (pos < f301.length) {
                                let tag; [tag, pos] = dv(f301, pos);
                                const fn = Number(tag >> 3n), wt = Number(tag & 7n);
                                if (wt === 0) { let v; [v, pos] = dv(f301, pos); if (fn === 3) cursor = v.toString(); }
                                else if (wt === 2) { let len; [len, pos] = dv(f301, pos); pos += Number(len); }
                                else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                            }
                            return { convId: reqConvId, cursor: cursor };
                        }""", list(body))
                        if result and result.get("cursor") and result["cursor"] != "0":
                            req_conv_id = result.get("convId", "")
                            if req_conv_id == conv_id:
                                self._captured_api_cursor = result["cursor"]
                                print(f"  [*] 捕获到 API cursor: {result['cursor']} (conv_id 匹配)")
                                cursor_captured_event.set()
                            else:
                                print(f"  [!] 忽略不匹配的 API 请求: conv_id={req_conv_id} (期望 {conv_id})")
                except Exception:
                    pass

        self.page.on("request", capture_api_request)

        # 3. 重新加载聊天页面（SDK 内存缓存随页面销毁而清除）
        print(f"  [*] 重新加载聊天页面...")
        await self.page.goto(CHAT_URL, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # 4. 重新点击目标会话（触发 SDK 从 API 加载消息）
        print(f"  [*] 重新点击会话: {clean_name}...")
        clicked = False
        conv_items = await self.page.query_selector_all(SEL_CONV_ITEM)
        for item in conv_items:
            title_el = await item.query_selector(SEL_CONV_TITLE)
            if title_el:
                title_text = (await title_el.inner_text()).strip()
                if clean_name in title_text or title_text in conv_name:
                    await item.click()
                    clicked = True
                    print(f"  [*] 重新点击会话: {title_text}")
                    break
        if not clicked:
            print(f"  [!] 重新加载后未找到会话「{clean_name}」，跳过 API 模式")
            self.page.remove_listener("request", capture_api_request)
            return

        # 5. 等待 SDK 发出 API 请求（缓存已清，应该很快）
        print(f"  [*] 等待 SDK 发出 API 请求...")
        try:
            await asyncio.wait_for(cursor_captured_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            # 如果 15 秒内没捕获到，尝试轻微滚动触发
            print(f"  [*] 未立即捕获到，尝试滚动触发...")
            for i in range(50):
                await self.page.evaluate("""() => {
                    const el = document.querySelector('[class*="messageMessageListlist"]');
                    if (el) el.scrollTop += 3000;
                }""")
                await asyncio.sleep(0.3)
                if self._captured_api_cursor:
                    break

        self.page.remove_listener("request", capture_api_request)

        if not self._captured_api_cursor:
            print(f"  [!] 未能捕获 API cursor，回退到滚动模式...")
            scroll_saved, _ = await self._scroll_up_and_collect(conv_id, incremental=self.incremental)
            print(f"  [+] 共保存 {scroll_saved} 条消息")
            return

        # 6. 注入 API 工具 + 直接 API 获取全部消息
        total_saved = await self._api_fetch_all_messages(
            conv_id, self._captured_api_cursor, incremental=self.incremental
        )
        print(f"  [+] 共保存 {total_saved} 条消息")

    async def _clear_sdk_cache(self):
        """清除 IM SDK 的本地缓存（localStorage/sessionStorage/IndexedDB），保留 cookies。

        SDK 会在 IndexedDB 和 localStorage 中缓存消息数据。
        清除后重新加载页面，SDK 将被迫从 API 重新拉取消息。
        """
        await self.page.evaluate("""async () => {
            // 清除 localStorage 和 sessionStorage
            try { localStorage.clear(); } catch(e) {}
            try { sessionStorage.clear(); } catch(e) {}

            // 删除所有 IndexedDB 数据库
            try {
                const dbs = await indexedDB.databases();
                for (const db of dbs) {
                    if (db.name) {
                        indexedDB.deleteDatabase(db.name);
                    }
                }
            } catch(e) {
                // indexedDB.databases() 可能不支持，逐个尝试已知的数据库名
                const knownDbs = ['im_sdk', 'im_db', 'douyin_im', 'bytedance_im'];
                for (const name of knownDbs) {
                    try { indexedDB.deleteDatabase(name); } catch(e2) {}
                }
            }
        }""")
        print(f"  [*] 已清除 localStorage/sessionStorage/IndexedDB")

    async def _get_scroll_info(self):
        """获取滚动容器的详细状态。"""
        return await self.page.evaluate(f"""() => {{
            const el = document.querySelector('{SEL_MSG_LIST}');
            if (!el) return null;
            // 找到真正可滚动的元素（可能是 msg list 本身或其父/子元素）
            let scrollEl = el;
            if (el.scrollHeight <= el.clientHeight) {{
                // 尝试父元素
                if (el.parentElement && el.parentElement.scrollHeight > el.parentElement.clientHeight) {{
                    scrollEl = el.parentElement;
                }}
            }}
            return {{
                scrollTop: scrollEl.scrollTop,
                scrollHeight: scrollEl.scrollHeight,
                clientHeight: scrollEl.clientHeight,
                scrollable: scrollEl.scrollHeight > scrollEl.clientHeight,
                tagName: scrollEl.tagName,
                className: (scrollEl.className || '').substring(0, 60),
            }};
        }}""")

    async def _js_scroll(self, delta_y):
        """用 JS scrollBy 直接操作滚动容器（比 mouse.wheel 更可靠）。"""
        await self.page.evaluate(f"""() => {{
            const el = document.querySelector('{SEL_MSG_LIST}');
            if (!el) return;
            let scrollEl = el;
            if (el.scrollHeight <= el.clientHeight && el.parentElement) {{
                scrollEl = el.parentElement;
            }}
            scrollEl.scrollBy({{ top: {delta_y}, behavior: 'instant' }});
        }}""")

    async def _inject_api_tools(self):
        """注入 protobuf 编解码 + IM API 调用工具到页面中。"""
        await self.page.evaluate("""() => {
            if (window.__imApi) return; // 已注入
            // ── protobuf 编码 ──
            function encodeVarint(value) {
                const bytes = [];
                let v = typeof value === 'bigint' ? value : BigInt(value);
                do {
                    let b = Number(v & 0x7Fn);
                    v >>= 7n;
                    if (v > 0n) b |= 0x80;
                    bytes.push(b);
                } while (v > 0n);
                if (bytes.length === 0) bytes.push(0);
                return new Uint8Array(bytes);
            }
            function encodeTag(fn, wt) { return encodeVarint((fn << 3) | wt); }
            function encodeString(fn, s) {
                const e = new TextEncoder().encode(s);
                return concatArrays([encodeTag(fn, 2), encodeVarint(e.length), e]);
            }
            function encodeVarintField(fn, v) { return concatArrays([encodeTag(fn, 0), encodeVarint(v)]); }
            function encodeBytes(fn, d) { return concatArrays([encodeTag(fn, 2), encodeVarint(d.length), d]); }
            function concatArrays(arrs) {
                const t = arrs.reduce((s, a) => s + a.length, 0);
                const r = new Uint8Array(t); let o = 0;
                for (const a of arrs) { r.set(a, o); o += a.length; }
                return r;
            }

            // ── protobuf 解码 ──
            function decodeVarint(buf, pos) {
                let result = 0, shift = 0;
                while (pos < buf.length) {
                    const b = buf[pos++]; result |= (b & 0x7F) << shift;
                    if ((b & 0x80) === 0) break; shift += 7; if (shift > 35) break;
                }
                return [result, pos];
            }
            function decodeVarintBig(buf, pos) {
                let result = 0n, shift = 0n;
                while (pos < buf.length) {
                    const b = buf[pos++]; result |= BigInt(b & 0x7F) << shift;
                    if ((b & 0x80) === 0) break; shift += 7n;
                }
                return [result, pos];
            }
            function extractField(buf, targetField) {
                let pos = 0;
                while (pos < buf.length) {
                    let tag; [tag, pos] = decodeVarint(buf, pos);
                    const fn = tag >> 3, wt = tag & 7;
                    if (wt === 0) { let v; [v, pos] = decodeVarintBig(buf, pos); }
                    else if (wt === 2) { let len; [len, pos] = decodeVarint(buf, pos); if (fn === targetField) return buf.slice(pos, pos + len); pos += len; }
                    else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                }
                return null;
            }

            function buildRequest(convId, cursor, timestamp) {
                const inner = concatArrays([
                    encodeString(1, convId), encodeVarintField(2, 1),
                    encodeVarintField(3, cursor), encodeVarintField(4, 1),
                    encodeVarintField(5, timestamp), encodeVarintField(6, 50),
                ]);
                const queryMsg = encodeBytes(301, inner);
                return concatArrays([
                    encodeVarintField(1, 301), encodeVarintField(2, 10027),
                    encodeString(3, '0.1.6'), encodeString(4, ''),
                    encodeVarintField(5, 3), encodeVarintField(6, 0),
                    encodeString(7, 'fef1a80:p/lzg/store'),
                    encodeBytes(8, queryMsg), encodeString(9, '0'),
                    encodeString(11, 'douyin_pc'), encodeString(14, '360000'),
                    encodeVarintField(18, 1), encodeString(21, 'douyin_pc'),
                ]);
            }

            // 通用 protobuf 递归解析器（返回所有字段）
            function parseProto(buf, depth) {
                if (!depth) depth = 0;
                const fields = {}; let pos = 0;
                while (pos < buf.length) {
                    let tag; [tag, pos] = decodeVarint(buf, pos);
                    const fn = tag >> 3, wt = tag & 7;
                    if (fn === 0 || fn > 200) break;
                    if (wt === 0) {
                        let v; [v, pos] = decodeVarintBig(buf, pos);
                        fields['f'+fn] = v.toString();
                    } else if (wt === 2) {
                        let len; [len, pos] = decodeVarint(buf, pos);
                        if (pos + len > buf.length) break;
                        const slice = buf.slice(pos, pos+len);
                        // 尝试解码为 UTF-8 文本
                        let text = null;
                        try { text = new TextDecoder('utf-8', {fatal:true}).decode(slice); } catch {}
                        if (text !== null && text.length < 5000) {
                            fields['f'+fn] = text;
                        } else if (depth < 3 && len > 4) {
                            // 尝试递归解析为嵌套 protobuf
                            try {
                                const sub = parseProto(slice, depth + 1);
                                if (Object.keys(sub).length > 0) fields['f'+fn] = sub;
                            } catch {}
                        }
                        pos += len;
                    } else if (wt === 1) { pos += 8; }
                    else if (wt === 5) { pos += 4; }
                    else break;
                }
                return fields;
            }

            function parseMessage(buf) {
                const r = {}; let pos = 0;
                while (pos < buf.length) {
                    let tag; [tag, pos] = decodeVarint(buf, pos);
                    const fn = tag >> 3, wt = tag & 7;
                    if (fn === 0 || fn > 500) break;
                    if (wt === 0) { let v; [v, pos] = decodeVarintBig(buf, pos);
                        if (fn===3) r.server_id=v.toString(); else if (fn===4) r.created_at_us=v.toString();
                        else if (fn===5) r.order=v.toString(); else if (fn===7) r.sender_uid=v.toString();
                        else if (fn===6) r.type_code=Number(v); else if (fn===11) r.is_recalled=Number(v);
                        else if (fn===12) r.visible=Number(v);
                    } else if (wt === 2) { let len; [len, pos] = decodeVarint(buf, pos);
                        const slice = buf.slice(pos, pos+len);
                        if (fn===1) r.conv_id=new TextDecoder().decode(slice);
                        else if (fn===8) { try { r.content_json=new TextDecoder().decode(slice); } catch {} }
                        else if (fn===18) {
                            // Field 18: 引用/回复消息
                            // 结构: f1=被引用消息server_id, f2=JSON(content, nickname, refmsg_sec_uid, refmsg_content)
                            try {
                                const refProto = parseProto(slice, 0);
                                if (refProto.f1 && refProto.f2) {
                                    const refJson = JSON.parse(refProto.f2);
                                    r._ref_msg = {
                                        server_id: refProto.f1,
                                        content: refJson.content || '',
                                        nickname: refJson.nickname || '',
                                        sec_uid: refJson.refmsg_sec_uid || '',
                                        refmsg_content: refJson.refmsg_content || '',
                                    };
                                }
                            } catch {}
                        }
                        pos += len;
                    } else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                }
                return r;
            }

            function parseResponse(data) {
                const f6 = extractField(data, 6);
                if (!f6) return { msgs: [], hasMore: 0, nextTs: null };
                const f301 = extractField(f6, 301);
                if (!f301) return { msgs: [], hasMore: 0, nextTs: null };
                let pos = 0; const msgs = []; let nextTs = null, hasMore = 0;
                while (pos < f301.length) {
                    let tag; [tag, pos] = decodeVarint(f301, pos);
                    const fn = tag >> 3, wt = tag & 7;
                    if (wt === 0) { let v; [v, pos] = decodeVarintBig(f301, pos);
                        if (fn===2) nextTs=v.toString(); if (fn===3) hasMore=Number(v);
                    } else if (wt === 2) { let len; [len, pos] = decodeVarint(f301, pos);
                        if (fn===1) msgs.push(parseMessage(f301.slice(pos, pos+len)));
                        pos += len;
                    } else if (wt === 1) pos += 8; else if (wt === 5) pos += 4; else break;
                }
                return { msgs, nextTs, hasMore };
            }

            // ── API 调用 ──
            window.__imApi = {
                buildRequest, parseResponse,
                call: async function(convId, cursor, timestamp, retries = 3) {
                    for (let attempt = 0; attempt < retries; attempt++) {
                        try {
                            const result = await new Promise((resolve, reject) => {
                                const reqBody = buildRequest(convId, BigInt(cursor), BigInt(timestamp));
                                const xhr = new XMLHttpRequest();
                                xhr.open('POST', 'https://imapi.douyin.com/v1/message/get_by_conversation');
                                xhr.setRequestHeader('Content-Type', 'application/x-protobuf');
                                xhr.setRequestHeader('Accept', 'application/x-protobuf');
                                xhr.responseType = 'arraybuffer';
                                xhr.withCredentials = true;
                                xhr.timeout = 30000;
                                xhr.onload = () => resolve({ status: xhr.status, data: new Uint8Array(xhr.response) });
                                xhr.onerror = () => reject(new Error('XHR failed'));
                                xhr.ontimeout = () => reject(new Error('XHR timeout'));
                                xhr.send(reqBody.buffer);
                            });
                            return result;
                        } catch (e) {
                            if (attempt < retries - 1) {
                                const wait = (attempt + 1) * 3000;
                                console.log('[imApi] call attempt ' + (attempt+1) + '/' + retries + ' failed: ' + e.message + ', retry in ' + (wait/1000) + 's');
                                await new Promise(r => setTimeout(r, wait));
                            } else {
                                throw e;
                            }
                        }
                    }
                },
                fetchBatch: async function(convId, cursor, timestamp, maxPages) {
                    const allMsgs = [];
                    let ts = timestamp;
                    let hasMore = 1;
                    let consecutiveErrors = 0;
                    for (let i = 0; i < maxPages && hasMore; i++) {
                        try {
                            const r = await this.call(convId, cursor, ts);
                            if (r.status !== 200) {
                                console.log('[imApi] page ' + i + ': HTTP ' + r.status);
                                consecutiveErrors++;
                                if (consecutiveErrors >= 3) break;
                                await new Promise(r => setTimeout(r, 3000));
                                continue;
                            }
                            consecutiveErrors = 0;
                            const p = this.parseResponse(r.data);
                            if (!p.msgs || p.msgs.length === 0) break;
                            for (const m of p.msgs) allMsgs.push(m);
                            hasMore = p.hasMore;
                            ts = p.nextTs;
                            if (i % 10 === 9) await new Promise(r => setTimeout(r, 50));
                        } catch (e) {
                            console.log('[imApi] page ' + i + ' error: ' + e.message);
                            consecutiveErrors++;
                            if (consecutiveErrors >= 3) {
                                return { msgs: allMsgs, nextTs: ts, hasMore, error: e.message };
                            }
                            await new Promise(r => setTimeout(r, 3000));
                        }
                    }
                    return { msgs: allMsgs, nextTs: ts, hasMore };
                },
            };
        }""")

    async def _api_fetch_all_messages(self, conv_id, cursor, incremental=False):
        """用 API 直接获取从 cursor 开始的所有历史消息。cursor 来自滚动阶段捕获。"""
        await self._inject_api_tools()

        api_cursor = cursor
        next_ts = "9999999999999999"
        print(f"  [*] API 直取模式: cursor={api_cursor}, 从最新开始向旧获取")

        # 3. 增量模式：获取已有消息的最旧时间戳
        existing_count = 0
        existing_oldest_ts = None
        if incremental:
            row = self._db_conn.execute(
                "SELECT COUNT(*), MIN(timestamp) FROM messages WHERE conv_id = ?", (conv_id,)
            ).fetchone()
            existing_count = row[0] or 0
            existing_oldest_ts = row[1] if row[1] else None
            if existing_count:
                print(f"  [*] 增量模式: 已有 {existing_count} 条消息")

        # 4. 循环分页获取所有消息
        total_saved = 0
        total_fetched = 0
        batch_num = 0
        zero_saved_streak = 0  # 连续 saved=0 的批次计数
        pages_per_batch = 20  # 每批获取 20 页 = 1000 条
        has_more = True
        start_time = time.time()

        while has_more:
            batch_num += 1

            # 带重试的批量 API 调用
            batch_result = None
            for attempt in range(3):
                try:
                    batch_result = await self.page.evaluate("""async (args) => {
                        const [convId, cursor, ts, maxPages] = args;
                        return await window.__imApi.fetchBatch(convId, cursor, ts, maxPages);
                    }""", [conv_id, api_cursor, next_ts, pages_per_batch])
                    break
                except Exception as e:
                    if attempt < 2:
                        wait = (attempt + 1) * 5
                        print(f"  [!] batch #{batch_num} 失败 (attempt {attempt+1}/3): {e}")
                        print(f"  [*] 等待 {wait}s 后重试...")
                        await asyncio.sleep(wait)
                    else:
                        print(f"  [!] batch #{batch_num} 连续 3 次失败，停止")
                        has_more = False

            if batch_result and batch_result.get("error"):
                print(f"  [!] batch #{batch_num} JS 端报错: {batch_result['error']}")

            if not batch_result or not batch_result.get("msgs"):
                if has_more:
                    print(f"  [*] API 返回空结果，停止")
                break

            msgs = batch_result["msgs"]
            has_more = batch_result.get("hasMore", 0) == 1
            next_ts = batch_result.get("nextTs", next_ts)
            total_fetched += len(msgs)

            # 前3批打印引用消息统计
            if batch_num <= 3:
                ref_count = sum(1 for m in msgs if m.get("_ref_msg"))
                if ref_count:
                    print(f"  [debug] 发现 {ref_count} 条引用/回复消息")

            # 过滤掉不属于目标会话的消息（防止 cursor 错误导致拉到其他会话的数据）
            filtered_msgs = []
            for m in msgs:
                msg_conv_id = m.get("conv_id", "")
                if msg_conv_id and msg_conv_id != conv_id:
                    continue
                filtered_msgs.append(m)
            if len(filtered_msgs) < len(msgs):
                print(f"  [!] 过滤掉 {len(msgs) - len(filtered_msgs)} 条不属于当前会话的消息")
            msgs = filtered_msgs

            # 转换 API 消息格式 → _store_messages 期望的格式
            converted = []
            for m in msgs:
                content_json = m.get("content_json", "")
                # 解析 content JSON
                text = ""
                msg_type = "other"
                awe_type = -1
                image_src = None
                try:
                    cj = json.loads(content_json)
                    awe_type = cj.get("aweType", -1)
                    text = cj.get("text", "") or cj.get("description", "")
                    if awe_type in (500, 501, 507, 508, 510, 514, 516):
                        # 表情包/贴纸
                        msg_type = "emoji"
                        if not text:
                            text = cj.get("display_name") or "[表情]"
                        # URL 在 cj.url.url_list[0]
                        url_obj = cj.get("url")
                        if isinstance(url_obj, dict):
                            url_list = url_obj.get("url_list", [])
                            if url_list:
                                image_src = url_list[0] if isinstance(url_list[0], str) else None
                    elif awe_type in (2702, 2703, 2704):
                        # 图片消息
                        msg_type = "image"
                        if not text:
                            text = "[图片]"
                        # URL 在 cj.resource_url.large_url_list[0]
                        ru = cj.get("resource_url") or {}
                        for key in ("large_url_list", "medium_url_list", "origin_url_list", "thumb_url_list"):
                            ul = ru.get(key, [])
                            if ul and isinstance(ul[0], str):
                                image_src = ul[0]
                                break
                    elif awe_type == 700 or awe_type == 0:
                        msg_type = "text"
                    elif awe_type == 701 or awe_type == 703:
                        msg_type = "text"
                    elif awe_type in (11054, 11055, 11063, 11066, 11067, 11069, 11070):
                        # 分享视频/直播
                        msg_type = "share"
                        if not text:
                            text = cj.get("push_detail") or "[分享]"
                        # 封面图 在 cj.cover_url.url_list[0]
                        cover = cj.get("cover_url")
                        if isinstance(cover, dict):
                            ul = cover.get("url_list", [])
                            if ul and isinstance(ul[0], str):
                                image_src = ul[0]
                    elif awe_type in (11029, 10500, 10401):
                        # 分享商品/评论
                        msg_type = "share"
                        # aweType=10500: 引用视频评论，comment 字段包含评论内容
                        comment = cj.get("comment", "")
                        aweme_title = cj.get("aweme_title", "")
                        if comment:
                            text = comment
                        elif not text:
                            text = cj.get("push_detail") or aweme_title or "[分享]"
                    elif awe_type in (800, 801, 803):
                        msg_type = "share"
                        if not text:
                            text = "[分享]"
                    elif awe_type >= 100000:
                        msg_type = "other"
                        text = text or cj.get("push_detail") or "[系统消息]"
                    elif cj.get("resource_url") and cj.get("duration"):
                        # 语音消息：有 resource_url 和 duration
                        msg_type = "other"  # 保持 type=0，前端会检测 resource_url
                        dur_sec = round(cj["duration"] / 1000)
                        text = text or f"[语音 {dur_sec}秒]"
                    elif text:
                        msg_type = "text"
                    else:
                        msg_type = "other"
                        text = cj.get("push_detail") or cj.get("display_name") or content_json[:200]
                except (json.JSONDecodeError, AttributeError):
                    text = content_json
                    msg_type = "text"

                if not text and msg_type == "text":
                    text = content_json

                # 时间戳：serverId 是 snowflake ID，高32位是 Unix 秒时间戳
                server_id_int = int(m.get("server_id", "0"))
                timestamp_sec = server_id_int >> 32 if server_id_int > 0 else 0

                # order 用于排序：created_at_us 是单调递增的，用作排序键
                created_at_us = int(m.get("created_at_us", "0"))

                # 引用/回复消息
                ref_msg = m.get("_ref_msg")
                ref_msg_json = json.dumps(ref_msg, ensure_ascii=False) if ref_msg else None

                converted.append({
                    "server_id": m.get("server_id", ""),
                    "content": text,
                    "msg_type": msg_type,
                    "awe_type": awe_type,
                    "is_self": False,  # API 不直接给出，后面可从 sender_uid 判断
                    "sender_uid": m.get("sender_uid", ""),
                    "sender_name": "",  # API 不返回名字
                    "conversation_id": m.get("conv_id", conv_id),
                    "created_at": datetime.utcfromtimestamp(timestamp_sec).isoformat() + "Z" if timestamp_sec > 0 else "",
                    "order_high": created_at_us >> 32,
                    "order_low": created_at_us & 0xFFFFFFFF,
                    "image_src": image_src,
                    "visible": m.get("visible", 0),
                    "is_recalled": m.get("is_recalled", 0),
                    "content_json": content_json,
                    "ref_msg": ref_msg_json,
                })

            # 下载语音文件
            await self._download_voice_files(converted)

            newly_inserted = self._store_messages(converted, conv_id, batch_seq_start=0)
            total_saved += newly_inserted

            elapsed = time.time() - start_time
            speed = total_fetched / elapsed if elapsed > 0 else 0
            # 计算时间范围
            if converted:
                times = [c["created_at"] for c in converted if c["created_at"]]
                oldest_time = min(times)[:19] if times else "?"
            else:
                oldest_time = "?"

            print(
                f"  [*] batch #{batch_num}: fetched={len(msgs)} saved={newly_inserted} "
                f"total={total_fetched}/{total_saved} oldest={oldest_time} "
                f"speed={speed:.0f}msg/s elapsed={elapsed:.1f}s hasMore={has_more}"
            )

            # 增量模式：连续 2 批 saved=0 说明已追上历史，停止
            if incremental and existing_count > 0:
                if newly_inserted == 0:
                    zero_saved_streak += 1
                    if zero_saved_streak >= 2:
                        print(f"  [*] 增量模式: 连续 {zero_saved_streak} 批无新消息，已追上历史记录")
                        break
                else:
                    zero_saved_streak = 0

            if not has_more:
                print(f"  [*] 已到达聊天记录起点")
                break

        # 5. 归一化 seq
        print(f"  [*] 归一化消息序号 (按服务端排序)...")
        rows = self._db_conn.execute(
            "SELECT msg_id FROM messages WHERE conv_id = ? ORDER BY seq ASC",
            (conv_id,),
        ).fetchall()
        for new_seq, row in enumerate(rows, 1):
            self._db_conn.execute(
                "UPDATE messages SET seq = ? WHERE msg_id = ?",
                (new_seq, row[0]),
            )
        self._db_conn.commit()
        print(f"  [*] 已归一化 {len(rows)} 条消息的序号")

        elapsed = time.time() - start_time
        print(f"  [*] API 获取完成: {total_fetched} 条消息, {total_saved} 条新增, 耗时 {elapsed:.1f}s")
        return total_saved

    async def _scroll_up_and_collect(self, conv_id, incremental=False):
        """从底部向上滚动，每次滚动后读取可见消息并立即存库。

        incremental=True 时，遇到数据库中已有的消息就停止。

        Returns:
            (total_saved, scroll_stuck): 保存的消息条数 和 是否因滚动卡住退出
        """
        msg_list = await self.page.query_selector(SEL_MSG_LIST)
        if not msg_list:
            print(f"  [!] 未找到消息列表")
            return 0, False

        box = await msg_list.bounding_box()
        if not box:
            return 0, False

        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        await self.page.mouse.move(cx, cy)

        # 打印滚动容器信息
        scroll_info = await self._get_scroll_info()
        if scroll_info:
            print(f"  [*] 滚动容器: {scroll_info['tagName']}.{scroll_info['className'][:30]}")
            print(f"      scrollHeight={scroll_info['scrollHeight']} clientHeight={scroll_info['clientHeight']}")

        # 增量模式：加载已有消息ID
        existing_ids = set()
        if incremental:
            rows = self._db_conn.execute("SELECT msg_id FROM messages WHERE conv_id = ?", (conv_id,)).fetchall()
            existing_ids = {r[0] for r in rows}
            if existing_ids:
                print(f"  [*] 增量模式: 数据库已有 {len(existing_ids)} 条消息")

        total_saved = 0
        scroll_round = 0
        hit_existing = False
        scroll_hit_ceiling = False  # 是否因滚动到虚拟列表上限而退出
        prev_scroll_height = 0       # 追踪 scrollHeight 变化
        height_stable_rounds = 0     # scrollHeight 无变化的连续轮数
        scroll_stuck_rounds = 0      # scrollTop 连续没动的轮数（真正的滚动卡住）
        no_new_msg_rounds = 0        # 连续没有新消息的轮数
        height_at_window_start = 0   # 窗口起始时的 scrollHeight（检测大范围变化）
        prev_scroll_top = -1
        seq_counter = 1_000_000_000
        prev_oldest_time = None      # 追踪最旧消息时间是否在变化
        oldest_time_stable_rounds = 0  # 最旧时间不再变化的连续轮数

        while True:
            messages = await self._read_messages()
            si = await self._get_scroll_info()

            newly_inserted = 0
            if messages:
                if incremental and existing_ids:
                    for msg in messages:
                        msg_id = self._make_msg_id(conv_id, msg)
                        if msg_id in existing_ids:
                            hit_existing = True
                            break

                seq_counter -= 1000
                newly_inserted = self._store_messages(messages, conv_id, seq_counter)
                total_saved += newly_inserted

            scroll_round += 1

            if incremental and hit_existing:
                print(f"  [*] 增量模式: 已追上历史记录 (滚动 {scroll_round} 次, 新增 {total_saved} 条)")
                break

            # ── 读取滚动状态 ──
            cur_scroll_height = si["scrollHeight"] if si else 0
            cur_scroll_top = si["scrollTop"] if si else 99999
            client_h = si["clientHeight"] if si else 600

            # 滚动步长：取当前最顶部消息的 virtual_height（即一条消息的高度）
            top_msg_height = None
            if messages:
                indices = [m.get("virtual_index") for m in messages if m.get("virtual_index") is not None]
                if indices:
                    top_idx = max(indices)
                    top_msg = next((m for m in messages if m.get("virtual_index") == top_idx), None)
                    if top_msg:
                        top_msg_height = top_msg.get("virtual_height")
            scroll_step = int(top_msg_height) if top_msg_height and top_msg_height > 10 else 200

            # ── 追踪 scrollTop 是否真的在动（区分"dedup-stuck"和"scroll-stuck"）──
            # dedup-stuck: scrollTop 在移动但没新消息 → 正常，继续常规滚动
            # scroll-stuck: scrollTop 卡住不动 → 需要恢复策略
            if abs(cur_scroll_top - prev_scroll_top) < 10:
                scroll_stuck_rounds += 1
            else:
                scroll_stuck_rounds = 0

            # 追踪新消息（用于退出判断）
            if newly_inserted > 0:
                no_new_msg_rounds = 0
            else:
                no_new_msg_rounds += 1

            # 追踪最旧消息时间是否在变化（核心退出依据）
            # 如果滚动仍在加载更旧的消息，oldest_time 会持续减小
            # 当 oldest_time 不再变化，说明真正到顶了
            cur_oldest_time = None
            if messages:
                times = [m.get("created_at") for m in messages if m.get("created_at")]
                if times:
                    cur_oldest_time = min(times)
            if cur_oldest_time and cur_oldest_time == prev_oldest_time:
                oldest_time_stable_rounds += 1
            else:
                oldest_time_stable_rounds = 0
            prev_oldest_time = cur_oldest_time

            # scrollHeight 变化追踪：每50轮检查一次大窗口的变化
            # 虚拟列表的 scrollHeight 会缓慢漂移（每轮+100px），
            # 所以逐轮 < 200px 的检查会误判为"稳定"。改用大窗口检测。
            if scroll_round % 50 == 0:
                if abs(cur_scroll_height - height_at_window_start) < 500:
                    height_stable_rounds += 50
                else:
                    height_stable_rounds = 0
                height_at_window_start = cur_scroll_height
            prev_scroll_height = cur_scroll_height

            # ── 日志（含 index 范围）──
            idx_min = idx_max = None
            if messages:
                indices = [m.get("virtual_index") for m in messages if m.get("virtual_index") is not None]
                if indices:
                    idx_min, idx_max = min(indices), max(indices)

            if scroll_round % 20 == 0 or (scroll_stuck_rounds > 0 and scroll_round % 5 == 0):
                first_time = messages[0].get("created_at", "?")[:19] if messages else "?"
                last_time = messages[-1].get("created_at", "?")[:19] if messages else "?"
                idx_str = f"idx=[{idx_min}~{idx_max}]" if idx_min is not None else "idx=?"
                print(
                    f"  [*] #{scroll_round}: saved={total_saved} new={newly_inserted} "
                    f"msgs={len(messages)} {idx_str} time=[{first_time} ~ {last_time}] "
                    f"scrollTop={cur_scroll_top:.0f} stuck={scroll_stuck_rounds} noNew={no_new_msg_rounds} oldestStable={oldest_time_stable_rounds}"
                )

            # ── 退出条件 ──
            # 核心退出：最旧可见消息的时间戳不再变化（说明已到达聊天记录的起点）
            # 即使 DB dedup 导致 no_new_msg 一直增长，只要 oldest_time 还在变，就继续
            if oldest_time_stable_rounds >= 100:
                idx_info = f", 当前 idx=[{idx_min}~{idx_max}]" if idx_min is not None else ""
                print(
                    f"  [*] 已到达顶部: 最旧时间 {prev_oldest_time} 连续 {oldest_time_stable_rounds} 轮未变化 "
                    f"(共滚动 {scroll_round} 次, 保存 {total_saved} 条{idx_info})"
                )
                scroll_hit_ceiling = True
                break

            # 安全退出：scroll 真正卡住（scrollTop 不动）且没有新消息超过 60 轮
            if scroll_stuck_rounds >= 60 and no_new_msg_rounds >= 60:
                idx_info = f", 当前 idx=[{idx_min}~{idx_max}]" if idx_min is not None else ""
                print(
                    f"  [*] 已到达顶部: scroll卡住 {scroll_stuck_rounds} 轮且无新消息 "
                    f"(共滚动 {scroll_round} 次, 保存 {total_saved} 条{idx_info})"
                )
                scroll_hit_ceiling = True
                break

            # ── 滚动策略 ──
            # 默认：正常 wheel 上滚 1/3 屏
            # 等待 0.6~0.9 秒让虚拟列表开始渲染，content 未加载的由 _read_messages 重试
            await self.page.mouse.wheel(0, -scroll_step)
            await asyncio.sleep(0.4 + random.random() * 0.2)

            # 仅当 scroll 真正卡住时（scrollTop 不动）才触发恢复
            if scroll_stuck_rounds >= 5 and scroll_stuck_rounds < 15:
                # 阶段1: 重新定位鼠标 + 加大 wheel
                msg_list_now = await self.page.query_selector(SEL_MSG_LIST)
                if msg_list_now:
                    new_box = await msg_list_now.bounding_box()
                    if new_box:
                        await self.page.mouse.move(
                            new_box["x"] + new_box["width"] / 2,
                            new_box["y"] + new_box["height"] / 2,
                        )
                if scroll_stuck_rounds % 5 == 0:
                    print(f"  [*] scroll卡住 {scroll_stuck_rounds} 轮，重新定位鼠标 (scrollTop={cur_scroll_top:.0f})")
                await self.page.mouse.wheel(0, -scroll_step * 2)
                await asyncio.sleep(0.3)

            elif scroll_stuck_rounds >= 15 and scroll_stuck_rounds < 40:
                # 阶段2: JS scrollBy 回弹
                bounce = min(500 + (scroll_stuck_rounds - 15) * 200, 5000)
                if scroll_stuck_rounds % 10 == 0:
                    print(f"  [*] scroll卡住 {scroll_stuck_rounds} 轮，JS回弹 ↓{bounce} (scrollTop={cur_scroll_top:.0f})")
                await self._js_scroll(bounce)
                await asyncio.sleep(0.5)
                await self._js_scroll(-(bounce + scroll_step * 3))
                await asyncio.sleep(0.8 + random.random() * 0.3)

            elif scroll_stuck_rounds >= 40:
                # 阶段3: 直接设置 scrollTop
                if scroll_stuck_rounds % 10 == 0:
                    print(f"  [*] scroll卡住 {scroll_stuck_rounds} 轮，直接设置scrollTop (scrollTop={cur_scroll_top:.0f})")
                await self.page.evaluate(f"""() => {{
                    const el = document.querySelector('{SEL_MSG_LIST}');
                    if (!el) return;
                    let scrollEl = el;
                    if (el.scrollHeight <= el.clientHeight && el.parentElement) {{
                        scrollEl = el.parentElement;
                    }}
                    scrollEl.scrollTop = Math.max(0, scrollEl.scrollTop - {client_h * 3});
                }}""")
                await asyncio.sleep(1.5)

            prev_scroll_top = cur_scroll_top

        # 归一化 seq：按 orderInConversation (存在 seq 字段中) 排序后重新编号为 1,2,3...
        # orderInConversation 是服务端的精确排序值，比 rowid 更可靠
        print(f"  [*] 归一化消息序号 (按服务端排序)...")
        rows = self._db_conn.execute(
            "SELECT msg_id FROM messages WHERE conv_id = ? ORDER BY seq ASC",
            (conv_id,),
        ).fetchall()
        for new_seq, row in enumerate(rows, 1):
            self._db_conn.execute(
                "UPDATE messages SET seq = ? WHERE msg_id = ?",
                (new_seq, row[0]),
            )
        self._db_conn.commit()
        print(f"  [*] 已归一化 {len(rows)} 条消息的序号")

        return total_saved, scroll_hit_ceiling

    async def _read_messages(self):
        """Read all messages via React Fiber extraction for precise data.

        Traverses from [data-e2e="msg-item-content"] up the fiber tree to get
        the full message object with serverId, createdAt, orderInConversation, etc.
        Falls back to DOM parsing for messages where fiber extraction fails.
        """
        messages = await self.page.evaluate("""() => {
            const msgEls = document.querySelectorAll('[data-e2e="msg-item-content"]');
            const results = [];

            for (const el of msgEls) {
                const fiberKey = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
                if (!fiberKey) continue;

                // Go up 4 levels: div → div → div → Context.Provider → MessageComponent
                let fiber = el[fiberKey];
                let msg = null;
                for (let i = 0; i < 8 && fiber; i++) {
                    if (fiber.memoizedProps && fiber.memoizedProps.message &&
                        fiber.memoizedProps.message.serverId) {
                        msg = fiber.memoizedProps.message;
                        break;
                    }
                    fiber = fiber.return;
                }
                if (!msg) continue;

                // Parse content JSON
                let textContent = '';
                let msgType = 'text';
                let mediaUrl = null;
                let aweType = 0;

                try {
                    if (typeof msg.content === 'string' && msg.content.startsWith('{')) {
                        const c = JSON.parse(msg.content);
                        textContent = c.text || '';
                        aweType = c.aweType || 0;

                        if ([500,501,507,508,510,514,516].includes(aweType)) {
                            // 表情包/贴纸
                            msgType = 'emoji';
                            if (!textContent) textContent = c.display_name || '[表情]';
                            // URL 在 c.url.url_list[0]
                            const urlObj = c.url;
                            if (urlObj && urlObj.url_list && urlObj.url_list.length)
                                mediaUrl = urlObj.url_list[0];
                        } else if ([2702,2703,2704].includes(aweType)) {
                            // 图片消息
                            msgType = 'image';
                            if (!textContent) textContent = '[图片]';
                            const ru = c.resource_url || {};
                            const ul = ru.large_url_list || ru.medium_url_list || ru.origin_url_list || ru.thumb_url_list || [];
                            if (ul.length) mediaUrl = ul[0];
                        } else if (aweType === 700 || aweType === 0 || aweType === 701 || aweType === 703) {
                            msgType = 'text';
                        } else if ([11054,11055,11063,11066,11067,11069,11070].includes(aweType)) {
                            // 分享视频/直播
                            msgType = 'share';
                            if (!textContent) textContent = c.push_detail || c.description || '[分享]';
                            const cover = c.cover_url;
                            if (cover && cover.url_list && cover.url_list.length)
                                mediaUrl = cover.url_list[0];
                        } else if ([11029,10500,10401,800,801,803].includes(aweType)) {
                            msgType = 'share';
                            if (!textContent) textContent = c.push_detail || c.description || '[分享]';
                        } else if (aweType >= 100000) {
                            msgType = 'other';
                            if (!textContent) textContent = c.push_detail || c.tips || '[系统消息]';
                        } else if (textContent) {
                            msgType = 'text';
                        } else {
                            // Unknown content type, use raw
                            textContent = msg.content.slice(0, 200);
                            msgType = 'other';
                        }
                    } else if (typeof msg.content === 'string') {
                        textContent = msg.content;
                    }
                } catch(e) {
                    textContent = msg.content ? String(msg.content).slice(0, 200) : '';
                    msgType = 'other';
                }

                // Also get sender name from DOM (for group chats)
                let senderName = '';
                // Walk up from el to find the message box with sender info
                let parentBox = el.closest('[class*="messageMessageBoxmessageBox"]');
                if (parentBox) {
                    const nameEl = parentBox.querySelector('div[class*="avatarName"]') ||
                                   parentBox.querySelector('div[class*="MessageBoxGroupName"]') ||
                                   parentBox.querySelector('span[class*="GroupName"]') ||
                                   parentBox.querySelector('div[class*="MessageTitle"]');
                    if (nameEl) senderName = nameEl.textContent.trim();
                }

                // Get virtualItem info for scroll tracking
                let virtualIndex = null;
                let virtualHeight = null;
                let fiberUp = el[fiberKey];
                for (let d = 0; d < 15 && fiberUp; d++) {
                    const mp = fiberUp.memoizedProps;
                    if (mp && mp.virtualItem) {
                        virtualIndex = mp.virtualItem.index;
                        virtualHeight = mp.virtualItem.size ?? mp.virtualItem.height ?? null;
                        break;
                    }
                    fiberUp = fiberUp.return;
                }

                // Extract timestamp as ISO string
                let createdAtISO = null;
                try {
                    if (msg.createdAt) {
                        createdAtISO = (msg.createdAt instanceof Date)
                            ? msg.createdAt.toISOString()
                            : new Date(msg.createdAt).toISOString();
                    }
                } catch(e) {}

                // Ensure mediaUrl is always a string or null
                if (mediaUrl && typeof mediaUrl === 'object') {
                    mediaUrl = mediaUrl.url_list ? mediaUrl.url_list[0] : JSON.stringify(mediaUrl);
                }
                if (mediaUrl && typeof mediaUrl !== 'string') mediaUrl = null;

                // Get image from DOM if not in content JSON
                if (!mediaUrl && (msgType === 'emoji' || msgType === 'image')) {
                    const imgEl = el.querySelector('img[src*="douyinpic"], img[src*="byteimg"], img[class*="Emoji"], img[class*="Image"]');
                    if (imgEl && imgEl.src) mediaUrl = imgEl.src;
                }

                results.push({
                    server_id: msg.serverId || null,
                    content: textContent,
                    msg_type: msgType,
                    type_code: msg.type,
                    awe_type: aweType,
                    is_self: !!msg.isMyMessage,
                    sender_uid: msg.sender || '',
                    sender_name: senderName,
                    conversation_id: msg.conversationId || '',
                    created_at: createdAtISO,
                    order_high: msg.orderInConversation ? msg.orderInConversation.high : 0,
                    order_low: msg.orderInConversation ? msg.orderInConversation.low : 0,
                    image_src: mediaUrl,
                    visible: msg.visible !== false,
                    is_recalled: !!msg.isRecalled,
                    virtual_index: virtualIndex,
                    virtual_height: virtualHeight,
                });
            }

            return results;
        }""")

        # Filter out recalled/invisible messages, keep empty content with valid server_id
        messages = [m for m in messages if m.get("server_id") and not m.get("is_recalled")]
        return messages

    @staticmethod
    def _make_msg_id(conv_id, msg):
        """生成消息ID，优先使用 serverId（稳定唯一）。"""
        server_id = msg.get("server_id")
        if server_id:
            return f"srv_{server_id}"
        # fallback: 基于内容 hash
        image_src = msg.get("image_src") or ""
        content = msg.get("content", "")
        is_self = msg.get("is_self", False)
        sender = msg.get("sender_uid", "") or msg.get("sender", "")
        msg_hash = hashlib.md5(
            f"{conv_id}:{content}:{is_self}:{sender}:{image_src}".encode()
        ).hexdigest()
        return f"web_{msg_hash}"

    def _store_messages(self, messages, conv_id, batch_seq_start=0):
        """Store a batch of messages to the database immediately. Returns count of newly inserted rows."""
        conn = self._db_conn
        newly_inserted = 0

        for idx, msg in enumerate(messages):
            content = msg.get("content", "")
            if not content:
                continue

            msg_id = self._make_msg_id(conv_id, msg)

            # Sender: use real UID from fiber data
            sender_uid = msg.get("sender_uid", "")
            sender_name = msg.get("sender_name", "")
            if msg.get("is_self"):
                sender_name = sender_name or "__self__"
            if not sender_uid:
                sender_uid = hashlib.md5(
                    (sender_name or "unknown").encode()
                ).hexdigest()[:12]

            msg_type_map = {"text": 1, "emoji": 2, "image": 3, "share": 4, "other": 0}
            msg_type = msg_type_map.get(msg.get("msg_type", "text"), 0)

            # 图片和表情包都记录 media_url (ensure it's a string)
            raw_media = msg.get("image_src") if msg.get("msg_type") in ("image", "emoji", "share") else None
            media_url = str(raw_media) if raw_media and isinstance(raw_media, str) else None
            local_path = msg.get("local_path")

            # Timestamp: use precise createdAt from fiber (ISO string → Unix seconds)
            timestamp = 0
            created_at = msg.get("created_at")
            if created_at:
                try:
                    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    timestamp = int(dt.timestamp())
                except (ValueError, AttributeError):
                    pass

            # Seq: use orderInConversation (high << 32 | low) for precise ordering
            order_high = msg.get("order_high", 0) or 0
            order_low = msg.get("order_low", 0) or 0
            # Convert to a single sortable integer (use as seq)
            # order_high is the upper 32 bits, order_low is the lower 32 bits
            # We use (order_high * 2^32 + unsigned(order_low)) for sorting
            unsigned_low = order_low if order_low >= 0 else order_low + (1 << 32)
            seq = order_high * (1 << 32) + unsigned_low if (order_high or order_low) else (batch_seq_start + idx)

            ref_msg = msg.get("ref_msg")

            cursor = conn.execute(
                """INSERT OR IGNORE INTO messages
                   (msg_id, conv_id, sender_uid, sender_name, content, msg_type,
                    media_url, media_local_path, timestamp, seq, raw_data, ref_msg)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (msg_id, conv_id, sender_uid, sender_name, content, msg_type,
                 media_url, local_path, timestamp, seq,
                 json.dumps(msg, ensure_ascii=False), ref_msg),
            )
            if cursor.rowcount > 0:
                newly_inserted += 1
            elif ref_msg:
                # 已存在的消息：更新 ref_msg（如果新数据包含引用信息）
                conn.execute(
                    "UPDATE messages SET ref_msg = ? WHERE msg_id = ? AND (ref_msg IS NULL OR ref_msg = '')",
                    (ref_msg, msg_id),
                )

            if sender_uid and sender_name and sender_name != "__self__":
                upsert_user(conn, sender_uid, nickname=sender_name)

        update_conversation_stats(conn, conv_id)
        conn.commit()

        # 每 1000 条做一次 WAL checkpoint，防止 WAL 文件膨胀
        self._commit_counter = getattr(self, "_commit_counter", 0) + len(messages)
        if self._commit_counter >= 1000:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            self._commit_counter = 0

        return newly_inserted

    async def close(self):
        if self._db_conn:
            try:
                self._db_conn.commit()
                self._db_conn.close()
            except Exception:
                pass
            self._db_conn = None
        if self.context:
            await self.context.close()
        if self.pw:
            await self.pw.stop()
        print("[+] 浏览器已关闭")
