<template>
  <div ref="containerRef" class="search-container" :class="{ expanded: showResults }">
    <div class="search-input-wrap">
      <svg class="search-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/>
      </svg>
      <input
        v-model="query"
        placeholder="搜索聊天记录..."
        @input="onInput"
        @focus="showResults = results.length > 0"
        @keydown.escape="showResults = false"
      />
      <span v-if="query" class="search-clear" @click="clear">&#x2715;</span>
    </div>
    <div v-if="showResults" class="search-results">
      <div class="search-results-header">
        找到 {{ total }} 条结果
      </div>
      <div
        v-for="item in results"
        :key="item.msg_id"
        class="search-result-item"
        @click="showResults = false; $emit('navigate', item)"
      >
        <div class="result-conv">{{ item.sender_display_name || item.sender_name || '' }}</div>
        <div class="result-content" v-html="highlight(item.content)"></div>
        <div class="result-meta">
          <span>{{ item.conv_name || '未知会话' }}</span>
          <span>{{ formatTime(item.timestamp) }}</span>
        </div>
      </div>
      <div v-if="results.length === 0 && !loading" class="search-no-results">
        无匹配结果
      </div>
      <div v-if="loading" class="search-loading">搜索中...</div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onUnmounted } from 'vue'

defineEmits(['navigate'])

const query = ref('')
const results = ref([])
const total = ref(0)
const loading = ref(false)
const showResults = ref(false)
const containerRef = ref(null)
let debounceTimer = null

function onClickOutside(e) {
  if (containerRef.value && !containerRef.value.contains(e.target)) {
    showResults.value = false
  }
}
onMounted(() => document.addEventListener('click', onClickOutside))
onUnmounted(() => document.removeEventListener('click', onClickOutside))

async function search(q) {
  if (!q || q.length < 1) {
    results.value = []
    showResults.value = false
    return
  }
  loading.value = true
  showResults.value = true
  const res = await fetch(`/api/search?q=${encodeURIComponent(q)}&page_size=50`)
  const data = await res.json()
  results.value = data.items
  total.value = data.total
  loading.value = false
}

function onInput() {
  clearTimeout(debounceTimer)
  debounceTimer = setTimeout(() => search(query.value), 400)
}

function clear() {
  query.value = ''
  results.value = []
  showResults.value = false
}

function escapeHtml(text) {
  const div = document.createElement('div')
  div.textContent = text
  return div.innerHTML
}

function highlight(text) {
  if (!text || !query.value) return escapeHtml(text || '')
  const safe = escapeHtml(text)
  const escaped = query.value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return safe.replace(
    new RegExp(`(${escaped})`, 'gi'),
    '<mark style="background:var(--highlight);color:#000;padding:0 2px;border-radius:2px">$1</mark>'
  )
}

function formatTime(ts) {
  if (!ts) return ''
  return new Date(ts * 1000).toLocaleDateString('zh-CN', {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit'
  })
}
</script>

<style scoped>
.search-container {
  position: relative;
}

.search-input-wrap {
  display: flex;
  align-items: center;
  gap: 8px;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 6px 12px;
}
.search-input-wrap:focus-within {
  border-color: var(--accent);
}

.search-icon {
  width: 16px;
  height: 16px;
  color: var(--text-muted);
  flex-shrink: 0;
}

.search-input-wrap input {
  flex: 1;
  border: none;
  background: transparent;
  color: var(--text-primary);
  font-size: 13px;
  outline: none;
}

.search-clear {
  cursor: pointer;
  color: var(--text-muted);
  font-size: 14px;
}
.search-clear:hover {
  color: var(--text-primary);
}

.search-results {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  max-height: 400px;
  overflow-y: auto;
  background: var(--bg-secondary);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.3);
  z-index: 100;
}

.search-results-header {
  padding: 8px 14px;
  font-size: 12px;
  color: var(--text-muted);
  border-bottom: 1px solid var(--border-color);
}

.search-result-item {
  padding: 10px 14px;
  cursor: pointer;
  border-bottom: 1px solid var(--border-color);
  transition: background 0.15s;
}
.search-result-item:hover {
  background: var(--bg-tertiary);
}
.search-result-item:last-child {
  border-bottom: none;
}

.result-conv {
  font-size: 12px;
  color: var(--accent);
  margin-bottom: 3px;
}
.result-content {
  font-size: 13px;
  line-height: 1.4;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.result-meta {
  display: flex;
  justify-content: space-between;
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
}

.search-no-results, .search-loading {
  padding: 20px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
}
</style>
