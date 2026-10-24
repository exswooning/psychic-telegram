import { useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { fetchMe } from '@/api/controlPlane'

export const VIEWED_KEY = 'bitport.viewedAccount'

export const storedViewedAccount = (): number | undefined => {
  try { return Number(sessionStorage.getItem(VIEWED_KEY)) || undefined } catch { return undefined }
}

/** Which account a superadmin is looking at: ?account=<id>, else the tab's last choice.
 *  undefined = their own. Anyone who is not a superadmin always gets undefined. */
export function useViewedAccount(): number | undefined {
  const [params] = useSearchParams()
  const [isSuper, setIsSuper] = useState(false)
  useEffect(() => {
    let live = true
    fetchMe().then((m) => { if (live) setIsSuper(!!m.is_superadmin) }).catch(() => {})
    return () => { live = false }
  }, [])
  if (!isSuper) return undefined
  return Number(params.get('account')) || storedViewedAccount()
}
