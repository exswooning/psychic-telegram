/** The kinds of change a mirror cycle records, in the order an operator asks about them,
 *  and how a lag reads. Kept apart from the page so the page file exports only itself. */
export const KINDS: { key: string; label: string }[] = [
  { key: 'new', label: 'New' }, { key: 'edited', label: 'Edited' },
  { key: 'renamed', label: 'Renamed' }, { key: 'moved', label: 'Moved' },
  { key: 'sharing', label: 'Sharing' }, { key: 'deleted', label: 'Deleted' },
  { key: 'comment', label: 'Comments' }, { key: 'labels', label: 'Labels' },
  { key: 'restored', label: 'Restored' }, { key: 'recreated', label: 'Re-created' },
]

export const duration = (secs: number) => {
  if (secs < 90) return `${Math.round(secs)} s`
  const m = Math.round(secs / 60)
  if (m < 90) return `${m} min`
  const h = Math.floor(m / 60)
  if (h < 48) return `${h} h ${m % 60} min`
  return `${Math.floor(h / 24)} d ${h % 24} h`
}
