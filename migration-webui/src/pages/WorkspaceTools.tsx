import { useEffect, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { Box, Chip, InputAdornment, Paper, Stack, TextField, Typography } from '@mui/material'
import { ArrowForward, Search } from '@mui/icons-material'
import { fetchMe } from '@/api/controlPlane'
import { availableFeatures } from '@/workspaceFeatures'
import { useWorkspaceContext } from '@/components/WorkspaceContext'
export default function WorkspaceTools() {
  const ctx = useWorkspaceContext()
  const [account, setAccount] = useState(ctx?.account || null)
  const [query, setQuery] = useState(''), [group, setGroup] = useState('All tools')
  useEffect(() => { if (ctx?.account) setAccount(ctx.account); else fetchMe().then(setAccount).catch(() => {}) }, [ctx?.account])
  const features = availableFeatures(!!account?.is_superadmin, !!account?.seed_enabled).filter(f => f.path !== '/tools' && (group === 'All tools' || f.group === group) && `${f.label} ${f.description}`.toLowerCase().includes(query.toLowerCase()))
  return <Stack spacing={3}><Box><Typography variant="h2">All tools</Typography><Typography color="text.secondary" sx={{ mt: 1 }}>Everything available to your account, organized by task.</Typography></Box><TextField value={query} onChange={e => setQuery(e.target.value)} label="Find a tool" placeholder="Try hybrid email, sharing, or repair" InputProps={{ startAdornment: <InputAdornment position="start"><Search /></InputAdornment> }} sx={{ maxWidth: 560 }} /><Stack direction="row" gap={1} sx={{ flexWrap: 'wrap' }}>{['All tools', 'Start', 'Move', 'Review', 'Manage'].map(g => <Chip key={g} label={g} onClick={() => setGroup(g)} color={g === group ? 'primary' : 'default'} variant={g === group ? 'filled' : 'outlined'} />)}</Stack><Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', sm: 'repeat(2, 1fr)', xl: 'repeat(3, 1fr)' }, gap: 2 }}>{features.map(f => <Paper key={f.path} variant="outlined" component={RouterLink} to={f.path} sx={{ p: 3, color: 'inherit', textDecoration: 'none', display: 'flex', gap: 2, '&:hover': { bgcolor: 'action.hover', borderColor: 'primary.main' } }}><Box sx={{ flex: 1 }}><Typography sx={{ fontWeight: 500 }}>{f.label}</Typography><Typography variant="body2" color="text.secondary" sx={{ mt: 1 }}>{f.description}</Typography><Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 2 }}>{f.group}</Typography></Box><ArrowForward sx={{ fontSize: 18, color: 'text.secondary', mt: .5 }} /></Paper>)}</Box>{!features.length && <Typography color="text.secondary">No matching tools. Try another service or task.</Typography>}</Stack>
}
