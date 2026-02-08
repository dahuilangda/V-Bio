const loaded = new Map<string, Promise<void>>();

export function loadScript(src: string): Promise<void> {
  if (loaded.has(src)) {
    return loaded.get(src)!;
  }

  const promise = new Promise<void>((resolve, reject) => {
    const existing = document.querySelector(`script[src=\"${src}\"]`) as HTMLScriptElement | null;
    if (existing) {
      if (existing.dataset.loaded === 'true') {
        resolve();
        return;
      }
      existing.addEventListener('load', () => resolve(), { once: true });
      existing.addEventListener('error', () => reject(new Error(`Script load failed: ${src}`)), {
        once: true
      });
      return;
    }

    const script = document.createElement('script');
    script.src = src;
    script.async = true;
    script.onload = () => {
      script.dataset.loaded = 'true';
      resolve();
    };
    script.onerror = () => reject(new Error(`Script load failed: ${src}`));
    document.head.appendChild(script);
  });

  loaded.set(src, promise);
  return promise;
}

export function loadStylesheet(href: string): void {
  if (document.querySelector(`link[href=\"${href}\"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = href;
  document.head.appendChild(link);
}
