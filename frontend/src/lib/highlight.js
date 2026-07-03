// Shared search-highlight helpers (used by MessageList + SearchBar).

// Escape text to safe HTML via a detached element's textContent.
export function escapeHtml(text) {
  const div = document.createElement('div')
  div.textContent = text
  return div.innerHTML
}

// Return `text` as HTML with case-insensitive matches of `query` wrapped in a
// <mark>. HTML is escaped first, then the query is regex-escaped. When there is
// no query (or no text) the escaped text is returned unchanged.
export function highlightText(text, query) {
  const safe = escapeHtml(text || '')
  if (!text || !query) return safe
  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return safe.replace(
    new RegExp(`(${escaped})`, 'gi'),
    '<mark style="background:var(--highlight);color:#000;padding:0 2px;border-radius:2px">$1</mark>'
  )
}
