export type Mode = 'wipe' | 'remove' | 'delete_users' | 'repair'

export const COPY: Record<Mode, { title: string; verb: string; warn: string }> = {
  wipe: {
    title: 'Wipe tenant data',
    verb: 'Wipe data',
    warn: 'Deletes the seeded Drive files, mail, calendar events, contacts '
        + 'and tasks. The Cloud project, the delegation grant and the saved '
        + 'configuration are kept, so the tenant stays ready to seed or '
        + 'migrate again.',
  },
  // The one action here that adds rather than removes. It sits with these
  // because it is per-tenant and needs the same admin password, not because
  // it is dangerous -- hence the plain colour on its button.
  repair: {
    title: 'Repair console setup',
    verb: 'Repair',
    warn: 'Re-pastes the delegation grant (picking up any scope added since '
        + 'this tenant was set up) and configures the Chat app. Both are '
        + 'console steps with no API, done once during setup and unreachable '
        + 'afterwards. Adds nothing and deletes nothing.',
  },
  delete_users: {
    title: 'Delete all users',
    verb: 'Delete users',
    warn: 'Deletes every migrated account in this tenant, not just its '
        + 'data, and invalidates the ledger that described them. A deleted '
        + 'Workspace address stays reserved for 20 days, so recreating one '
        + 'under the same name fails until it ages out.',
  },
  remove: {
    title: 'Remove tenant setup',
    verb: 'Remove setup',
    warn: 'Deletes the data AND the Cloud project, revokes the delegation '
        + 'grant, and forgets the configuration. Setting this tenant up '
        + 'again means a fresh sign-in and a fresh grant.',
  },
}

/** Only the teardown half signs in to Google. A wipe uses the service
 *  account already on file, so demanding a password for it would be asking
 *  for a credential nothing is going to use. */
export const needsPassword = (mode: Mode) =>
  mode === 'remove' || mode === 'repair'
