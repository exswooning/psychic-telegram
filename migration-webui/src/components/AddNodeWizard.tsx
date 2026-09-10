/**
 * Add a machine — the guided half of the Nodes page.
 *
 * What was here before offered exactly one route: a node_setup.sh command
 * with three placeholders left in it, driving a fresh Ubuntu box over SSH
 * from a third machine. That is the wrong shape for the common case. The
 * machine somebody actually wants to add is the laptop they are sitting at,
 * and they are not going to SSH into it from the coordinator.
 *
 * So this asks the three things that genuinely vary — where this
 * coordinator is reachable from THERE, which OS, which tenant — and hands
 * back one line to paste, with the token already in it. Nothing is gated
 * behind a Next button: every field is visible and the command rewrites as
 * you type, because the command IS the output and hiding it behind steps
 * would only add clicks.
 *
 * The address field is not a convenience. install.sh sets
 * BITPORT_PUBLIC_ORIGIN only when a public domain was given, so a LAN or
 * tailnet install reports nothing here and the operator is the only one who
 * knows the answer.
 */
import React, { useState } from 'react'
import {
  Alert, Box, Button, Chip, Collapse, Link, Stack, TextField, ToggleButton,
  ToggleButtonGroup, Typography,
} from '@mui/material'
import {
  ContentCopy as CopyIcon, Visibility as ShowIcon,
  VisibilityOff as HideIcon, Check as CheckIcon,
} from '@mui/icons-material'
import type { NodeJoinDetails } from '@/api/controlPlane'

const RAW = 'https://raw.githubusercontent.com/exswooning/psychic-telegram/workspace-migrator'

export type NodeOs = 'unix' | 'windows'

/**
 * The one line to paste on the joining machine.
 *
 * Both forms pass their settings through the ENVIRONMENT rather than as
 * arguments. Two reasons, and the second is the load-bearing one: a piped
 * script has no argv to receive flags on, and argv is readable by every
 * process on the box while this line carries a live credential.
 */
export const joinCommand = (
  os: NodeOs, coordinator: string, token: string, account: number,
): string => {
  const c = coordinator.trim().replace(/\/+$/, '') || '<coordinator-url>'
  const t = token || '<node-token>'
  if (os === 'windows') {
    // irm|iex rather than a downloaded .ps1: Windows PowerShell defaults to
    // an execution policy of Restricted, which refuses to run a script FILE.
    // A piped string is not a file and is not subject to it.
    return [
      `$env:BITPORT_COORDINATOR='${c}'`,
      `$env:BITPORT_NODE_TOKEN='${t}'`,
      `$env:BITPORT_ACCOUNT='${account}'`,
      `irm ${RAW}/install_node.ps1 | iex`,
    ].join('; ')
  }
  return [
    `BITPORT_COORDINATOR='${c}' \\`,
    `BITPORT_NODE_TOKEN='${t}' \\`,
    `BITPORT_ACCOUNT='${account}' \\`,
    `  bash -c "$(curl -fsSL ${RAW}/install_node.sh)"`,
  ].join('\n')
}

/**
 * Taking it off again.
 *
 * Handed out as a command for the same reason the join is: this page never
 * reaches into a node. fleet_agent.py's own reasoning -- a control plane
 * that could reach into its nodes would need credentials for every machine
 * holding service-account keys for both tenants, which turns a dashboard
 * into a lateral-movement path across the whole migration.
 *
 * Defaults to keeping the keys and the ledger. Removing a node is routine;
 * destroying the credentials for somebody's tenant is not, and the two
 * should not share a button.
 */
export const removeCommand = (os: NodeOs, purge: boolean): string => {
  if (os === 'windows') {
    return [
      "$env:BITPORT_UNINSTALL_YES='1'",
      ...(purge ? ["$env:BITPORT_UNINSTALL_PURGE='1'"] : []),
      `irm ${RAW}/uninstall_node.ps1 | iex`,
    ].join('; ')
  }
  // `bash -c "<script>" name args` still passes arguments -- a plain
  // `curl | bash` cannot, which is why the join command uses env vars.
  return `bash -c "$(curl -fsSL ${RAW}/uninstall.sh)" bitport --yes`
    + (purge ? ' --purge' : '')
}

const Step: React.FC<{ n: number; title: string; children: React.ReactNode }> =
  ({ n, title, children }) => (
    <Stack direction="row" spacing={2} sx={{ mb: 2.5 }}>
      <Box sx={{ width: 24, height: 24, borderRadius: '50%', flexShrink: 0,
                 bgcolor: 'primary.main', color: 'primary.contrastText',
                 display: 'grid', placeItems: 'center',
                 fontSize: 12, fontWeight: 700, mt: 0.25 }}>{n}</Box>
      <Box sx={{ flex: 1, minWidth: 0 }}>
        <Typography variant="subtitle2" sx={{ fontWeight: 700, mb: 1 }}>
          {title}
        </Typography>
        {children}
      </Box>
    </Stack>
  )

export const AddNodeWizard: React.FC<{
  join: NodeJoinDetails
  revealed: boolean
  onReveal: () => void
}> = ({ join, revealed, onReveal }) => {
  const [os, setOs] = useState<NodeOs>('unix')
  const [account, setAccount] = useState(7)
  // window.location.origin is a reasonable guess only when you are already
  // browsing the coordinator by an address the other machine can also use.
  const [addr, setAddr] = useState(join.coordinatorUrl || window.location.origin)
  const [copied, setCopied] = useState(false)
  const [removing, setRemoving] = useState(false)
  const [purge, setPurge] = useState(false)

  const loopback = /:8090\/?$/.test(addr.trim())
  const localOnly = /^https?:\/\/(localhost|127\.0\.0\.1)/i.test(addr.trim())
  const cmd = joinCommand(os, addr, revealed ? join.token : '', account)

  const copy = () => {
    navigator.clipboard?.writeText(cmd)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 2000)
  }

  return (
    <Box data-testid="add-node-wizard">
      <Step n={1} title="Where can the new machine reach this coordinator?">
        <TextField size="small" fullWidth value={addr}
                   onChange={(e) => setAddr(e.target.value)}
                   placeholder="http://192.168.1.50:81"
                   inputProps={{ 'data-testid': 'node-addr' }} />
        <Typography variant="caption" color="text.secondary"
                    sx={{ display: 'block', mt: 0.75 }}>
          The address <em>that</em> machine can use — a LAN address on the
          same WiFi, a tailnet address from anywhere. Use the port the
          installer reported for Caddy (80, or 81 if 80 was taken).
        </Typography>
        {loopback && (
          <Alert severity="error" sx={{ mt: 1 }} data-testid="addr-loopback">
            8090 is the API&apos;s own loopback port — it listens on
            127.0.0.1 only, so nothing outside this machine can reach it.
            Use the Caddy port; it proxies through.
          </Alert>
        )}
        {localOnly && !loopback && (
          <Alert severity="warning" sx={{ mt: 1 }} data-testid="addr-localhost">
            localhost means <em>the new machine itself</em> once it runs
            this. Put the address other machines see.
          </Alert>
        )}
      </Step>

      <Step n={2} title="What is it running?">
        <ToggleButtonGroup exclusive size="small" value={os}
                           onChange={(_, v) => v && setOs(v)}>
          <ToggleButton value="unix" data-testid="os-unix">Linux / macOS</ToggleButton>
          <ToggleButton value="windows" data-testid="os-windows">Windows</ToggleButton>
        </ToggleButtonGroup>
        {os === 'windows' && (
          <Typography variant="caption" color="text.secondary"
                      sx={{ display: 'block', mt: 0.75 }}>
            Runs start to finish with no prompts: it needs no admin rights,
            installs Python itself if missing, and needs no second shell.
          </Typography>
        )}
      </Step>

      <Step n={3} title="Which tenant will it work on?">
        <TextField size="small" type="number" value={account}
                   onChange={(e) => setAccount(Number(e.target.value) || 0)}
                   sx={{ width: 140 }} label="Account id"
                   inputProps={{ 'data-testid': 'node-account', min: 1 }} />
      </Step>

      <Step n={4} title="Run this on the new machine">
        <Stack direction="row" spacing={1} sx={{ mb: 1 }}>
          <Button size="small" onClick={onReveal}
                  startIcon={revealed ? <HideIcon /> : <ShowIcon />}
                  data-testid="reveal-token">
            {revealed ? 'Hide token' : 'Show token'}
          </Button>
          <Button size="small" variant={copied ? 'text' : 'outlined'}
                  disabled={!revealed}
                  startIcon={copied ? <CheckIcon /> : <CopyIcon />}
                  onClick={copy} data-testid="copy-command">
            {copied ? 'Copied' : 'Copy'}
          </Button>
          {!revealed && (
            <Chip size="small" variant="outlined" label="token hidden"
                  data-testid="token-hidden" />
          )}
        </Stack>
        <Box component="pre" data-testid="join-command"
             sx={{ fontSize: 11, p: 1.5, bgcolor: 'action.hover', m: 0,
                   borderRadius: 1, overflowX: 'auto', whiteSpace: 'pre-wrap',
                   fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
          {cmd}
        </Box>
        {!revealed && (
          <Typography variant="caption" color="text.secondary"
                      sx={{ display: 'block', mt: 0.75 }}>
            Reveal the token to fill it in. It is kept off screen by default
            so this page can be shared or screenshotted without leaking a
            live credential.
          </Typography>
        )}
      </Step>

      <Box sx={{ mt: 2, pt: 2, borderTop: '1px solid', borderColor: 'divider' }}>
        <Button size="small" onClick={() => setRemoving((v) => !v)}
                data-testid="toggle-remove">
          {removing ? 'Hide' : 'Remove Bitport from a machine'}
        </Button>
        <Collapse in={removing}>
          <Box sx={{ mt: 1.5 }}>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
              Run this on the machine you are removing — same as joining,
              this page never reaches into one. It uses the OS picked above.
            </Typography>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1.5 }}>
              <ToggleButtonGroup exclusive size="small" value={purge}
                                 onChange={(_, v) => v !== null && setPurge(v)}>
                <ToggleButton value={false} data-testid="keep-data">
                  Keep keys &amp; ledger
                </ToggleButton>
                <ToggleButton value={true} data-testid="purge-data">
                  Delete everything
                </ToggleButton>
              </ToggleButtonGroup>
            </Stack>
            {purge && (
              <Alert severity="error" sx={{ mb: 1.5 }} data-testid="purge-warning">
                This deletes the service-account keys and the migration
                ledger. New keys mean re-doing domain-wide delegation on both
                tenants by hand, and without the ledger a resumed migration
                re-copies every Drive file it already moved. The script asks
                you to type PURGE before it does it.
              </Alert>
            )}
            <Box component="pre" data-testid="remove-command"
                 sx={{ fontSize: 11, p: 1.5, bgcolor: 'action.hover', m: 0,
                       borderRadius: 1, overflowX: 'auto', whiteSpace: 'pre-wrap',
                       fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace' }}>
              {removeCommand(os, purge)}
            </Box>
            <Button size="small" startIcon={<CopyIcon />} sx={{ mt: 1 }}
                    data-testid="copy-remove"
                    onClick={() => navigator.clipboard?.writeText(removeCommand(os, purge))}>
              Copy
            </Button>
            <Typography variant="caption" color="text.secondary"
                        sx={{ display: 'block', mt: 1 }}>
              Run it with no flags first and it only prints what would go.
            </Typography>
          </Box>
        </Collapse>
      </Box>

      <Alert severity="info" sx={{ mt: 2 }}>
        <Typography variant="body2" sx={{ mb: 0.5 }}>
          Two things this deliberately does not do for you:
        </Typography>
        <Typography variant="body2" component="div">
          • The tenant&apos;s <strong>service-account keys</strong> are not
          served by this API. An endpoint handing them to anything holding a
          node token would make that token equivalent to the keys for the
          whole tenant — the installer prints the copy command at the end.
          <br />
          • <strong>Drive across two machines duplicates files.</strong> Each
          node keeps its own ledger, so Drive&apos;s duplicate check cannot
          see the other machine&apos;s work. Gmail is safe — it asks the
          target for the Message-ID. See{' '}
          <Link href="https://github.com/exswooning/psychic-telegram/blob/workspace-migrator/MULTINODE.md"
                target="_blank" rel="noopener">MULTINODE.md</Link>.
        </Typography>
      </Alert>
    </Box>
  )
}

export default AddNodeWizard
