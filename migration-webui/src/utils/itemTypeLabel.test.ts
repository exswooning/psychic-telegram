import { describe, it, expect } from 'vitest'
import { itemTypeLabel } from './itemTypeLabel'

describe('item type labels', () => {
  it('says email for mail and chat message for chat, never a bare "message"', () => {
    expect(itemTypeLabel('message')).toBe('email')
    expect(itemTypeLabel('draft')).toBe('email draft')
    expect(itemTypeLabel('chat_message')).toBe('chat message')
  })
  it('reads any other type with spaces, and nothing as nothing', () => {
    expect(itemTypeLabel('comment_reply')).toBe('comment reply')
    expect(itemTypeLabel('file')).toBe('file')
    expect(itemTypeLabel(undefined)).toBe('')
  })
})
