const RAW = 'https://raw.githubusercontent.com/exswooning/psychic-telegram/workspace-migrator'

export type NodeOs = 'unix' | 'windows'

/**
 * The one line to run when you have a join code.
 *
 * Everything else -- which coordinator, which token, which tenant -- is
 * inside the code, so this is the same length whatever the answers are.
 * The coordinator serves the installer with those three baked in, deriving
 * its own address from the request rather than from configuration, because
 * BITPORT_PUBLIC_ORIGIN is empty on every LAN and tailnet install.
 */
export const codeCommand = (os: NodeOs, origin: string, code: string): string => {
  const o = origin.replace(/\/+$/, '')
  const c = code || '<code>'
  return os === 'windows'
    ? `irm ${o}/api/v2/j/${c} | iex`
    : `curl -fsSL "${o}/api/v2/j/${c}?sh=true" | bash`
}

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
