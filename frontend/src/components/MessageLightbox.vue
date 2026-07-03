<template>
  <div v-if="modelValue" class="lightbox-overlay" @click.self="close">
    <img class="lightbox-img" :src="modelValue" @click.self="close" />
    <button class="lightbox-close" @click="close">×</button>
  </div>
</template>

<script setup>
import { onMounted, onUnmounted } from 'vue'

// Fullscreen image overlay. Controlled via v-model: the src to show, or null.
const props = defineProps({ modelValue: { type: String, default: null } })
const emit = defineEmits(['update:modelValue'])

function close() {
  emit('update:modelValue', null)
}
function onKey(e) {
  if (e.key === 'Escape' && props.modelValue) close()
}
onMounted(() => window.addEventListener('keydown', onKey))
onUnmounted(() => window.removeEventListener('keydown', onKey))
</script>

<style scoped>
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
</style>
