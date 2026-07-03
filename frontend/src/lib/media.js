// Resolve a stored avatar path to a servable URL (used by MessageList +
// ConversationList). Local 'avatars/…' paths are served under /media/; full
// http(s) URLs pass through; anything else has no avatar (initial-letter fallback).
export function resolveAvatarUrl(url) {
  if (!url) return null
  if (url.startsWith('avatars/')) return `/media/${url}`
  if (url.startsWith('http')) return url
  return null
}
