<template>
  <div class="conv-list">
    <div class="conv-header">
      <h2>会话</h2>
      <span class="conv-count">{{ total }}</span>
    </div>
    <div class="conv-search">
      <input
        v-model="searchQuery"
        placeholder="搜索会话..."
        @input="onSearch"
      />
    </div>
    <div class="conv-items" ref="listRef">
      <div
        v-for="conv in conversations"
        :key="conv.conv_id"
        class="conv-item"
        :class="{ active: conv.conv_id === activeId }"
        @click="$emit('select', conv)"
      >
        <div class="conv-avatar">
          <img v-if="getConvAvatar(conv)" :src="getConvAvatar(conv)" @error="e => e.target.style.display='none'" />
          <span v-else>{{ (conv.name || '?')[0] }}</span>
        </div>
        <div class="conv-info">
          <div class="conv-name">{{ conv.name || '未命名' }}</div>
          <div class="conv-meta">
            <span>{{ conv.message_count || 0 }} 条消息</span>
          </div>
        </div>
      </div>
      <div v-if="conversations.length === 0" class="conv-empty">
        暂无会话数据
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, reactive, onMounted } from 'vue'

const props = defineProps({
  activeId: String,
})
defineEmits(['select'])

const conversations = ref([])
const total = ref(0)
const searchQuery = ref('')
const usersMap = reactive({})  // uid -> { nickname, avatar_url }
let searchTimeout = null

async function fetchUsers() {
  try {
    const res = await fetch('/api/users')
    const users = await res.json()
    for (const u of users) {
      usersMap[u.uid] = u
    }
  } catch {}
}

function getConvAvatar(conv) {
  // 从 participant_uids 找到非自己的参与者头像
  try {
    const uids = JSON.parse(conv.participant_uids || '[]')
    const selfUid = localStorage.getItem('selfUid')
    const otherUid = uids.find(u => u !== selfUid) || uids[0]
    if (otherUid && usersMap[otherUid]?.avatar_url) {
      const url = usersMap[otherUid].avatar_url
      if (url.startsWith('avatars/')) return `/media/${url}`
      if (url.startsWith('http')) return url
    }
  } catch {}
  // fallback: 遍历 usersMap，找到昵称匹配会话名的用户
  for (const uid in usersMap) {
    const u = usersMap[uid]
    if (u.nickname && conv.name && conv.name.includes(u.nickname) && u.avatar_url) {
      const url = u.avatar_url
      if (url.startsWith('avatars/')) return `/media/${url}`
      if (url.startsWith('http')) return url
    }
  }
  return null
}

async function fetchConversations(search = '') {
  const params = new URLSearchParams({ page_size: '200' })
  if (search) params.set('search', search)
  const res = await fetch(`/api/conversations?${params}`)
  const data = await res.json()
  conversations.value = data.items
  total.value = data.total
}

function onSearch() {
  clearTimeout(searchTimeout)
  searchTimeout = setTimeout(() => {
    fetchConversations(searchQuery.value)
  }, 300)
}

onMounted(async () => {
  await fetchUsers()
  fetchConversations()
})
</script>

<style scoped>
.conv-list {
  display: flex;
  flex-direction: column;
  height: 100%;
  background: var(--bg-secondary);
  border-right: 1px solid var(--border-color);
}

.conv-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 16px;
  border-bottom: 1px solid var(--border-color);
}
.conv-header h2 {
  font-size: 16px;
  font-weight: 600;
}
.conv-count {
  background: var(--accent);
  color: white;
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 10px;
}

.conv-search {
  padding: 8px 12px;
}
.conv-search input {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--border-color);
  border-radius: 6px;
  background: var(--bg-primary);
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
}
.conv-search input:focus {
  border-color: var(--accent);
}

.conv-items {
  flex: 1;
  overflow-y: auto;
}

.conv-item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  cursor: pointer;
  transition: background 0.15s;
}
.conv-item:hover {
  background: var(--bg-tertiary);
}
.conv-item.active {
  background: var(--bg-tertiary);
  border-left: 3px solid var(--accent);
}

.conv-avatar {
  width: 40px;
  height: 40px;
  border-radius: 50%;
  background: var(--bg-tertiary);
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  font-weight: 600;
  flex-shrink: 0;
  color: var(--accent);
  overflow: hidden;
}
.conv-avatar img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}
.conv-avatar span {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
}

.conv-info {
  flex: 1;
  min-width: 0;
}
.conv-name {
  font-size: 14px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.conv-meta {
  font-size: 12px;
  color: var(--text-muted);
  margin-top: 2px;
}

.conv-empty {
  padding: 30px;
  text-align: center;
  color: var(--text-muted);
  font-size: 14px;
}
</style>
