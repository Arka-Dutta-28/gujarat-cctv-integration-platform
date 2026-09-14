import { useEffect, useState } from 'react'
import { authHeaders } from './api'

/**
 * An image fetched with the session token.
 *
 * A plain `<img src>` cannot carry an `Authorization` header, so with
 * authentication on, every evidence crop in the alert console rendered as a
 * broken image. Found by logging in as the demo account and looking at the
 * screen, which is the only way this class of defect shows up.
 *
 * The alternative — accepting the token as a query parameter — was rejected:
 * tokens in URLs end up in access logs, browser history and `Referer` headers,
 * and a crop is 8 kB, so fetching it properly costs nothing worth having.
 */
export function AuthedImage(
  { src, alt, className }: { src: string; alt: string; className?: string },
) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let revoked = false
    let url: string | null = null
    setFailed(false)

    fetch(src, { headers: authHeaders() })
      .then((res) => (res.ok ? res.blob() : Promise.reject(new Error(String(res.status)))))
      .then((blob) => {
        if (revoked) return
        url = URL.createObjectURL(blob)
        setObjectUrl(url)
      })
      .catch(() => !revoked && setFailed(true))

    return () => {
      revoked = true
      // Object URLs are held by the document until revoked; a console left open
      // for a shift would otherwise accumulate every crop it ever showed.
      if (url) URL.revokeObjectURL(url)
      setObjectUrl(null)
    }
  }, [src])

  if (failed) return <div className={`${className ?? ''} empty`} title="Crop unavailable">—</div>
  if (!objectUrl) return <div className={`${className ?? ''} empty`} />
  return <img className={className} src={objectUrl} alt={alt} />
}
