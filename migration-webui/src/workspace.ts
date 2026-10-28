/** Build the new interface separately; the installed /app bundle stays intact. */
export const workspaceUI = import.meta.env.VITE_WORKSPACE_UI === '1'
export const appBase = (import.meta.env.BASE_URL || '/app/').replace(/\/$/, '')
export const appLink = (path: string) => `${appBase}${path.startsWith('/') ? path : `/${path}`}`
