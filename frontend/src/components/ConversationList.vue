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
        <button
          class="conv-delete"
          title="删除该会话数据"
          @click.stop="requestDelete(conv)"
        >×</button>
      </div>
      <div v-if="conversations.length === 0" class="conv-empty">
        暂无会话数据
      </div>
    </div>

    <!-- Custom confirm modal -->
    <div v-if="pendingDelete" class="modal-backdrop" @click.self="cancelDelete">
      <div class="modal-box">
        <div class="modal-title">删除会话</div>
        <div class="modal-body">
          确定删除会话「<strong>{{ pendingDelete.name || '未命名' }}</strong>」的所有数据？
          <div class="modal-sub">共 {{ pendingDelete.message_count || 0 }} 条消息，此操作不可恢复。</div>
        </div>
        <div class="modal-actions">
          <button class="btn btn-cancel" @click="cancelDelete" :disabled="deleting">取消</button>
          <button class="btn btn-danger" @click="confirmDelete" :disabled="deleting">
            {{ deleting ? '删除中...' : '删除' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted } from 'vue'

const props = defineProps({
  activeId: String,
})
const emit = defineEmits(['select', 'deleted'])

const conversations = ref([])
const total = ref(0)
const searchQuery = ref('')
const pendingDelete = ref(null)
const deleting = ref(false)
let searchTimeout = null

function getConvAvatar(conv) {
  const url = conv.avatar_url
  if (!url) return null
  if (url.startsWith('avatars/')) return `/media/${url}`
  if (url.startsWith('http')) return url
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

function requestDelete(conv) {
  pendingDelete.value = conv
}

function cancelDelete() {
  if (deleting.value) return
  pendingDelete.value = null
}

async function confirmDelete() {
  const conv = pendingDelete.value
  if (!conv) return
  deleting.value = true
  try {
    // Use POST alias to avoid reverse proxies that block DELETE
    const res = await fetch(`/api/conversations/${encodeURIComponent(conv.conv_id)}/delete`, {
      method: 'POST',
    })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      alert(`删除失败：${res.status} ${body}`)
      return
    }
    conversations.value = conversations.value.filter(c => c.conv_id !== conv.conv_id)
    total.value = Math.max(0, total.value - 1)
    emit('deleted', conv.conv_id)
    pendingDelete.value = null
  } catch (e) {
    alert(`删除失败：${e.message || e}`)
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
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

.conv-delete {
  flex-shrink: 0;
  width: 22px;
  height: 22px;
  border: none;
  border-radius: 50%;
  background: transparent;
  color: var(--text-muted);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s, background 0.15s, color 0.15s;
  display: flex;
  align-items: center;
  justify-content: center;
}
.conv-item:hover .conv-delete {
  opacity: 1;
}
.conv-delete:hover {
  background: #e53935;
  color: #fff;
}

.conv-empty {
  padding: 30px;
  text-align: center;
  color: var(--text-muted);
  font-size: 14px;
}

/* Confirm modal */
.modal-backdrop {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.55);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 2000;
  animation: fadeIn 0.15s ease;
}
@keyframes fadeIn {
  from { opacity: 0; }
  to { opacity: 1; }
}

.modal-box {
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  min-width: 320px;
  max-width: 90vw;
  box-shadow: 0 10px 40px rgba(0, 0, 0, 0.4);
  overflow: hidden;
  animation: popIn 0.18s ease;
}
@keyframes popIn {
  from { transform: scale(0.92); opacity: 0; }
  to { transform: scale(1); opacity: 1; }
}

.modal-title {
  padding: 16px 20px;
  font-size: 16px;
  font-weight: 600;
  color: var(--text-primary);
  border-bottom: 1px solid var(--border-color);
}

.modal-body {
  padding: 20px;
  font-size: 14px;
  color: var(--text-primary);
  line-height: 1.6;
}
.modal-body strong {
  color: var(--accent);
  word-break: break-all;
}
.modal-sub {
  margin-top: 8px;
  font-size: 12px;
  color: var(--text-muted);
}

.modal-actions {
  display: flex;
  gap: 10px;
  padding: 12px 20px 18px;
  justify-content: flex-end;
}
.btn {
  padding: 8px 18px;
  border: none;
  border-radius: 6px;
  font-size: 14px;
  font-weight: 500;
  cursor: pointer;
  transition: filter 0.15s, background 0.15s;
}
.btn:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.btn-cancel {
  background: var(--bg-tertiary);
  color: var(--text-primary);
}
.btn-cancel:hover:not(:disabled) {
  filter: brightness(1.15);
}
.btn-danger {
  background: #e53935;
  color: #fff;
}
.btn-danger:hover:not(:disabled) {
  background: #c62828;
}
</style>
