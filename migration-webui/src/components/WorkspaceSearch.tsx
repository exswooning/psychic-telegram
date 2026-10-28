import { useEffect, useRef, useState } from 'react'
import { Button, Box, Dialog, DialogContent, InputBase, List, ListItemButton, ListItemText, Typography, IconButton, InputAdornment } from '@mui/material'
import { Search, Close, ArrowForward } from '@mui/icons-material'
import { useNavigate } from 'react-router-dom'
import { findFeatures } from '@/workspaceFeatures'
export default function WorkspaceSearch({ operator, seed }: { operator: boolean; seed: boolean }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const results = findFeatures(query, operator, seed)
  const list = useRef<HTMLUListElement>(null)
  const navigate = useNavigate()
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); setOpen(v => !v) } }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])
  const close = () => { setOpen(false); setQuery('') }
  return <>
    <Button aria-label="Find a tool" onClick={() => setOpen(true)} startIcon={<Search />} sx={{ color: 'text.secondary', bgcolor: 'action.hover', justifyContent: 'flex-start', px: { xs: 1, sm: 2.5 }, minWidth: { xs: 40, sm: 200 }, maxWidth: 590, flex: { xs: 0, sm: 1 }, height: 48, borderRadius: '28px', fontSize: 16 }}>
      <Box component="span" sx={{ display: { xs: 'none', sm: 'inline' }, flex: 1, textAlign: 'left' }}>Search Bitport</Box>
      <Box component="kbd" sx={{ display: { xs: 'none', sm: 'inline' }, fontSize: 11, border: '1px solid', borderColor: 'divider', borderRadius: 1, px: .6 }}>⌘ K</Box>
    </Button>
    <Dialog open={open} onClose={close} fullWidth maxWidth="sm" aria-labelledby="tool-search-title">
      <Box sx={{ px: 3, pt: 2.5, display: 'flex', alignItems: 'center' }}><Typography id="tool-search-title" variant="h6" sx={{ flex: 1 }}>Find a tool</Typography><IconButton aria-label="Close tool search" onClick={close}><Close /></IconButton></Box>
      <DialogContent sx={{ pt: 1 }}>
        <InputBase autoFocus fullWidth value={query} onChange={e => setQuery(e.target.value)} placeholder="Try sharing, hybrid email, or repair" inputProps={{ 'aria-label': 'Search all tools' }} startAdornment={<InputAdornment position="start"><Search /></InputAdornment>}
          onKeyDown={e => { if (e.key === 'ArrowDown') { e.preventDefault(); list.current?.querySelector<HTMLElement>('[role="button"]')?.focus() } if (e.key === 'Enter' && results.length === 1) { navigate(results[0].path); close() } }} sx={{ p: 1.5, bgcolor: 'action.hover', borderRadius: 2, mb: 1 }} />
        <Typography variant="caption" color="text.secondary">{results.length} tools available to your account</Typography>
        <List ref={list} sx={{ maxHeight: '55vh', overflowY: 'auto' }}>
          {results.map(f => <ListItemButton key={f.path} onClick={() => { navigate(f.path); close() }} sx={{ borderRadius: 2, mb: .5 }}><ListItemText primary={f.label} secondary={f.description} primaryTypographyProps={{ fontWeight: 500 }} /><ArrowForward sx={{ fontSize: 18, ml: 2, color: 'text.secondary' }} /></ListItemButton>)}
        </List>
        {!results.length && <Typography sx={{ py: 3 }} color="text.secondary">No matching tools. Try a service name or task.</Typography>}
      </DialogContent>
    </Dialog>
  </>
}
