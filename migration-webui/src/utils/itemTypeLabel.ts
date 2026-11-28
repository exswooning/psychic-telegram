/**
 * What to call a ledger item type on screen. The ledger's own names stay the keys everywhere (filters, ids,
 * API); only the words a person reads change. "message" said nothing about whether it was email or chat, and
 * next to Chat it read as chat (operator, 8 Nov 2026): email is "email", chat is "chat message".
 */
const LABEL: Record<string, string> = {
  message: 'email',
  draft: 'email draft',
  thread: 'email thread',
  label: 'email label',
  chat_message: 'chat message',
  chat_space: 'chat space',
}

export const itemTypeLabel = (type?: string | null): string =>
  type ? (LABEL[type] ?? type.replace(/_/g, ' ')) : ''
