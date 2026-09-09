import { useEffect, useRef } from 'react';
import { useLocation, useNavigationType } from 'react-router-dom';

/**
 * Route-change scroll policy for the document scroller.
 *
 * - PUSH / REPLACE navigations start the next page at the top (SPA route swaps
 *   never reset the window scroll on their own — without this, a link clicked
 *   at the bottom of a long list lands mid-page on the next screen).
 * - POP navigations (browser back/forward) keep the browser's own scroll
 *   restoration.
 * - Same-path navigations (search-param-only changes, e.g. workspace sub-tabs)
 *   keep the current scroll position: the user is re-configuring the same view.
 */
export function shouldResetScroll(
  prevPathname: string | null,
  pathname: string,
  navigationType: string
): boolean {
  if (prevPathname === pathname) return false;
  return navigationType !== 'POP';
}

export function ScrollToTop() {
  const { pathname } = useLocation();
  const navigationType = useNavigationType();
  const prevPathname = useRef<string | null>(null);

  useEffect(() => {
    const shouldReset = shouldResetScroll(prevPathname.current, pathname, navigationType);
    prevPathname.current = pathname;
    if (!shouldReset) return;
    window.scrollTo(0, 0);
  }, [pathname, navigationType]);

  return null;
}
