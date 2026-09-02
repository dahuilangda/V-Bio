/**
 * Multi-viewport UI audit harness (own dev environment).
 *
 * Logs in via the local auth API, seeds the session into localStorage, then walks the key
 * pages (login / projects / task list per workflow / api access) at phone/tablet/desktop
 * sizes, capturing screenshots plus a horizontal-overflow report for layout review.
 *
 * Usage: node scripts/ui-size-audit.mjs [outDir]
 */
import { chromium } from 'playwright-core';
import { mkdirSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const OUT = resolve(process.argv[2] || 'ui-audit');
mkdirSync(OUT, { recursive: true });

const BASE = process.env.AUDIT_BASE || 'http://localhost:5173';
const EXECUTABLE =
  process.env.AUDIT_CHROMIUM ||
  `${process.env.HOME}/.cache/ms-playwright/chromium_headless_shell-1234/chrome-headless-shell-linux64/chrome-headless-shell`;

const SIZES = [
  { name: 'phone', width: 375, height: 812 },
  { name: 'tablet', width: 768, height: 1024 },
  { name: 'desktop', width: 1440, height: 900 },
];

const login = async () => {
  const res = await fetch(`${BASE}/vbio-api/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ identifier: 'uitest', password: 'VbioUiTest#2026' }),
  });
  if (!res.ok) throw new Error(`login failed: ${res.status}`);
  const { session } = await res.json();
  return session;
};

const overflowReport = (page) =>
  page.evaluate(() => {
    const vw = document.documentElement.clientWidth;
    const offenders = [];
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.width > 0 && (r.right > vw + 1 || r.left < -1)) {
        const cs = getComputedStyle(el);
        if (cs.position === 'fixed') continue;
        const cls = String(el.className || '');
        offenders.push({
          tag: el.tagName.toLowerCase(),
          cls: cls.slice(0, 80),
          left: Math.round(r.left),
          right: Math.round(r.right),
          width: Math.round(r.width),
        });
      }
    }
    return {
      vw,
      scrollWidth: document.documentElement.scrollWidth,
      hasHorizontalScroll: document.documentElement.scrollWidth > vw + 1,
      offenders: offenders.slice(0, 12),
    };
  });

const run = async () => {
  const session = await login();
  const browser = await chromium.launch({ executablePath: EXECUTABLE, headless: true });
  const report = {};
  const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
  await page.goto(`${BASE}/login`, { waitUntil: 'domcontentloaded' });
  await page.evaluate((s) => localStorage.setItem('vbio_session', JSON.stringify(s)), session);

  // Discover this user's projects from the page-rendered list.
  await page.goto(`${BASE}/projects`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(600);
  const cards = await page.locator('a[href^="/projects/"]').all();
  const projectPaths = [];
  for (const card of cards.slice(0, 5)) {
    const href = await card.getAttribute('href');
    if (href && !projectPaths.includes(href)) projectPaths.push(href);
  }
  const targets = [
    { name: 'login', path: '/login', sizes: SIZES },
    { name: 'projects', path: '/projects', sizes: SIZES },
  ];
  // task lists for up to three projects; the api view needs a project whose workflow supports it
  for (const [i, path] of projectPaths.slice(0, 3).entries()) {
    targets.push({ name: `tasks-${i + 1}`, path: `${path}/tasks`, sizes: SIZES });
  }

  for (const t of targets) {
    for (const size of t.sizes) {
      await page.setViewportSize({ width: size.width, height: size.height });
      await page.goto(`${BASE}${t.path}`, { waitUntil: 'networkidle' });
      await page.waitForTimeout(500);
      report[`${t.name}@${size.name}`] = await overflowReport(page);
      await page.screenshot({ path: resolve(OUT, `${t.name}-${size.name}.png`) });
    }
  }

  // The ?view=api param only takes effect on projects whose workflow supports API access
  // (prediction / virtual_screening / affinity); anything else renders the task list. Probe the
  // audited projects until the API Access heading appears, so the screenshots show the builder.
  let apiTarget = null;
  await page.setViewportSize({ width: 1440, height: 900 });
  for (const path of projectPaths) {
    await page.goto(`${BASE}${path}/tasks?view=api`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(800);
    if (await page.locator('h1', { hasText: 'API Access' }).count()) {
      apiTarget = path;
      break;
    }
  }
  if (apiTarget) {
    console.log('api-access project:', apiTarget);
    report['api-access@desktop'] = await overflowReport(page);
    await page.screenshot({ path: resolve(OUT, 'api-access-desktop.png') });
    await page.setViewportSize({ width: 375, height: 812 });
    await page.goto(`${BASE}${apiTarget}/tasks?view=api`, { waitUntil: 'networkidle' });
    await page.waitForTimeout(800);
    report['api-access@phone'] = await overflowReport(page);
    await page.screenshot({ path: resolve(OUT, 'api-access-phone.png') });
  } else {
    console.log('api-access: no supporting project found');
  }

  writeFileSync(resolve(OUT, 'overflow-report.json'), JSON.stringify(report, null, 2));
  const bad = Object.entries(report).filter(([, v]) => v.hasHorizontalScroll);
  console.log('audited views:', Object.keys(report).length);
  console.log('horizontal-scroll views:', bad.map(([k]) => k).join(', ') || 'none');
  for (const [k, v] of bad) {
    console.log(`  ${k}: +${v.scrollWidth - v.vw}px`, v.offenders.slice(0, 3));
  }
  await browser.close();
};

run().catch((err) => {
  console.error('AUDIT FAILED:', err.message);
  process.exit(1);
});
