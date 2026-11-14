import React, { useCallback, useEffect, useState } from 'react'
import { Alert, Box, Button, Card, CardContent, Checkbox, Chip, FormControlLabel, Stack, TextField, Typography } from '@mui/material'
import { Link as RouterLink } from 'react-router-dom'
import ReasonCodeDialog from '@/components/ReasonCodeDialog'
import { fetchErrorResolution, fetchErrorCase, errorCaseAction, configureErrorResolution, connectErrorRunner, revokeErrorRunner } from '@/api/controlPlane'
import type { ErrorResolutionView, ErrorCaseDetail } from '@/api/controlPlane'

const labels: Record<string,string> = { QUEUED:'Queued for investigation', QUALIFICATION_QUEUED:'Queued for qualification', FIXING:'Preparing a fix', TESTING:'Testing',
  WAITING_QUALIFICATION:'Qualification needed', WAITING_DEPLOYMENT:'Waiting to deploy', DEPLOYED:'Deployed; needs recheck',
  RECHECKING:'Rechecking', RESOLVED:'Verified resolved', OWNER_ACTION:'Account or owner action', FAILED:'Needs attention',
  RECOVERING:'Recovery being observed', DETECTED:'Detected' }

function download(name: string, value: unknown) {
  const url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}))
  const a=document.createElement('a'); a.href=url; a.download=name; a.click(); URL.revokeObjectURL(url)
}

export default function ErrorResolution() {
  const [view,setView]=useState<ErrorResolutionView|null>(null)
  const [error,setError]=useState('')
  const [notice,setNotice]=useState('')
  const [filter,setFilter]=useState('')
  const [showResolved,setShowResolved]=useState(false)
  const [detail,setDetail]=useState<ErrorCaseDetail|null>(null)
  const [busy,setBusy]=useState(false)
  const [action,setAction]=useState<{kind:string; id?:number}|null>(null)
  const [daily,setDaily]=useState(3)
  const [allAccounts,setAllAccounts]=useState(false)
  const load=useCallback(()=>fetchErrorResolution().then(r=>{setView(r);setDaily(r.policy.daily_limit);setError('')}).catch(e=>setError(String(e))),[])
  useEffect(()=>{load();const t=setInterval(()=>{if(!document.hidden)load()},15000);return()=>clearInterval(t)},[load])
  const confirm=async(reason:string)=>{
    if(!action||!view)return
    setBusy(true);setError('');setNotice('')
    try {
      if(action.kind==='connect') {
        const config=await connectErrorRunner(reason,allAccounts)
        download(`bitport-coding-runner-${config.runnerId}.json`,config)
        setNotice('Scoped configuration downloaded once. Keep it private (chmod 600). Start incident_worker.py on your source laptop; no Google credentials are needed. Eligible Gmail quota-classification candidates are checked automatically. Deployment stays held until configured.')
      } else if(action.kind==='policy'||action.kind==='limit') {
        const r=await configureErrorResolution(action.kind==='limit'?!!view.policy.enabled:!view.policy.enabled,daily,reason)
        if(!r.ok)throw new Error(r.detail||'Policy change refused')
      } else if(action.kind==='revoke') {
        const r=await revokeErrorRunner(action.id!,reason)
        if(!r.ok)throw new Error(r.detail||'Revoke refused')
      } else {
        const r=await errorCaseAction(action.id!,action.kind,reason)
        if(!r.ok)throw new Error(r.detail||'Action refused')
        setNotice(r.detail||'Action accepted')
      }
      setAction(null);load()
    } catch(e){setError(String(e))}finally{setBusy(false)}
  }
  const shown=(view?.cases||[]).filter(c=>(showResolved||c.state!=='RESOLVED')&&
    `${c.title} ${c.service} ${c.category} ${c.reason} ${c.state}`.toLowerCase().includes(filter.toLowerCase()))
  const unresolved=(view?.cases||[]).filter(c=>c.state!=='RESOLVED').length
  return <Box>
    <Stack direction="row" gap={2} justifyContent="space-between" alignItems="center" sx={{mb:2}}>
      <Box><Typography variant="h4">Error resolution</Typography><Typography color="text.secondary">From a migration error to a tested fix and a fresh check.</Typography></Box>
      <Button onClick={load}>Refresh</Button>
    </Stack>
    {error&&<Alert severity="error" sx={{mb:2}}>{error}</Alert>}
    {notice&&<Alert severity="info" sx={{mb:2}}>{notice}</Alert>}
    <Stack direction={{xs:'column',md:'row'}} gap={2} sx={{mb:2}}>
      <Card variant="outlined" sx={{flex:1}}><CardContent><Typography color="text.secondary">Unresolved cases</Typography><Typography variant="h4">{view?unresolved:'—'}</Typography></CardContent></Card>
      <Card variant="outlined" sx={{flex:1}}><CardContent><Typography color="text.secondary">Automatic coding queue</Typography><Typography variant="h5">{view?(view.policy.enabled?'Enabled':'Paused'):'Loading'}</Typography><Typography variant="body2">{view?.policy.daily_limit??'—'} investigations per day across this server; at most two attempts per case.</Typography></CardContent></Card>
      <Card variant="outlined" sx={{flex:1}}><CardContent><Typography color="text.secondary">Coding worker</Typography><Typography variant="h5">{view?.runners.some(r=>r.enabled&&r.last_seen&&Date.now()-Date.parse(r.last_seen)<180000)?'Connected':'Not connected'}</Typography><Typography variant="body2">Migration and verification work has priority.</Typography></CardContent></Card>
    </Stack>
    <Alert severity="info" sx={{mb:2}}>A fix is resolved only after fresh, complete source/target checks pass. A sample, matching count, successful test or deployed commit is not 1:1 proof. Paused repair policies stay paused.</Alert>
    {view?.canManage&&<Card variant="outlined" sx={{mb:2}}><CardContent>
      <Typography variant="h6">Coding worker connection</Typography>
      <Typography variant="body2" color="text.secondary" sx={{mb:1}}>Runs on your source laptop in a separate checkout. Its scoped credential can read redacted incident bundles and report attempts; it cannot access Google data, start migrations or resolve findings.</Typography>
      <Stack direction={{xs:'column',sm:'row'}} gap={1} alignItems={{sm:'center'}}>
        {view.canManageGlobal&&<FormControlLabel label="All migration accounts" control={<Checkbox checked={allAccounts} onChange={e=>setAllAccounts(e.target.checked)}/>}/>}
        <Button variant="outlined" onClick={()=>setAction({kind:'connect'})}>Connect coding worker</Button>
        {view.canManageGlobal&&<><TextField label="Daily investigation limit" size="small" type="number" value={daily} inputProps={{min:1,max:20}} onChange={e=>setDaily(Number(e.target.value))}/>
        <Button disabled={!Number.isInteger(daily)||daily<1||daily>20} onClick={()=>setAction({kind:'limit'})}>Save limit</Button>
        <Button disabled={!Number.isInteger(daily)||daily<1||daily>20} onClick={()=>setAction({kind:'policy'})}>{view.policy.enabled?'Pause automatic coding':'Enable automatic coding'}</Button></>}
      </Stack>
      {view.runners.map(r=><Stack key={r.id} direction="row" gap={1} sx={{mt:1}} alignItems="center"><Typography variant="body2">{r.label} · {r.enabled?(r.last_seen?`Last seen ${new Date(r.last_seen).toLocaleString()}`:'Waiting for first connection'):'Revoked'} · {r.detail}</Typography>{!!r.enabled&&<Button size="small" onClick={()=>setAction({kind:'revoke',id:r.id})}>Revoke</Button>}</Stack>)}
    </CardContent></Card>}
    <Stack direction="row" gap={2} sx={{mb:2}}><TextField label="Filter cases" size="small" value={filter} onChange={e=>setFilter(e.target.value)} sx={{flex:1}}/><FormControlLabel label="Show resolved" control={<Checkbox checked={showResolved} onChange={e=>setShowResolved(e.target.checked)}/>}/></Stack>
    {view&&shown.length===0&&<Typography color="text.secondary">No matching cases recorded. New job errors are collected automatically; the existing incident history remains available in Final Report.</Typography>}
    <Stack gap={1.5}>{shown.map(c=><Card variant="outlined" key={c.id} data-testid={`error-case-${c.id}`}><CardContent>
      <Stack direction={{xs:'column',sm:'row'}} gap={1} justifyContent="space-between"><Box><Typography variant="h6">{c.title}</Typography><Typography variant="body2" color="text.secondary">{c.service} · {c.category} · {c.occurrences} occurrences · {c.affected_users} users · {c.affected_items} affected items</Typography></Box><Chip label={labels[c.state]||c.state} color={c.state==='RESOLVED'?'success':c.state==='FAILED'||c.state==='OWNER_ACTION'?'warning':'default'}/></Stack>
      <Typography sx={{my:1}}>{c.next_action}</Typography><Typography variant="caption" color="text.secondary">Observed version {c.version} · Last seen {new Date(c.last_seen).toLocaleString()}{c.candidate_commit?` · Candidate ${c.candidate_commit.slice(0,7)}`:''}</Typography>
      <Stack direction="row" gap={1} sx={{mt:1}}><Button onClick={()=>fetchErrorCase(c.id).then(setDetail).catch(e=>setError(String(e)))}>View evidence</Button>
      {view?.canManage&&c.state!=='RESOLVED'&&<Button onClick={()=>setAction({kind:'recheck',id:c.id})}>Recheck affected data</Button>}
      {view?.canManage&&['FAILED','WAITING_QUALIFICATION','DETECTED'].includes(c.state)&&['code','unknown'].includes(c.category)&&<Button onClick={()=>setAction({kind:'queue',id:c.id})}>{c.state==='WAITING_QUALIFICATION'&&c.candidate_commit?'Retry qualification':'Queue investigation'}</Button>}</Stack>
    </CardContent></Card>)}</Stack>
    {detail&&<Card variant="outlined" sx={{mt:2}}><CardContent><Stack direction="row" justifyContent="space-between"><Typography variant="h6">Case #{detail.id}: evidence</Typography><Button onClick={()=>setDetail(null)}>Close evidence</Button></Stack>
      <Typography variant="body2">The latest 20 recorded events and 200 affected items are shown. Full affected scope is retained for resolution checks.</Typography>
      {detail.history.map((h,i)=><Typography variant="body2" key={i} sx={{mt:1}}>{new Date(h.at).toLocaleString()} · {labels[h.state]||h.state}: {h.detail}</Typography>)}
      <Box component="pre" sx={{maxHeight:360,overflow:'auto',whiteSpace:'pre-wrap',fontSize:12}}>{JSON.stringify({affected:detail.affected,events:detail.events,attempts:detail.attempts},null,2)}</Box>
    </CardContent></Card>}
    <Button component={RouterLink} to="/logs" sx={{mt:2}}>Open job logs</Button>
    <ReasonCodeDialog open={!!action} busy={busy} title={action?.kind==='connect'?'Connect coding worker':action?.kind==='recheck'?'Recheck affected data':'Update error workflow'}
      description={action?.kind==='connect'?'Creates a persistent, revocable credential for redacted incident handoff and attempt reporting. Keep the downloaded configuration private.':action?.kind==='recheck'?'Starts a full scoped read-only check when the server is idle. No target repair is enabled by this action.':'Records this change in the operator audit log. Migration jobs and source protection remain unchanged.'}
      onCancel={()=>setAction(null)} onConfirm={confirm}/>
  </Box>
}
