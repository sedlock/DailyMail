// Visual and CSS regression QA for the rendered digest. DIAGNOSTICS ONLY.
//
// This never runs on the production path. `dailymail run-daily` has no browser
// dependency and must not acquire one: the digest is deterministic HTML built
// from stored state, and a browser is the wrong thing to put between the reader
// and their mail. What a browser *is* good for is answering questions a string
// assertion cannot -- "what colour does this paragraph actually compute to,
// after inheritance, in a viewport this narrow?" -- which is exactly the class
// of defect that shipped on 1 September 2026.
//
// Playwright is reused, never installed. Resolution order:
//   1. the repository's own node_modules (pinned in package.json)
//   2. the @playwright/cli bundle another project already installed globally
// If neither is present the script says so and exits 2 rather than downloading
// a second browser stack.
//
//   node tools/qa/visual-regression.mjs --page artifacts/qa/pages \
//        --screenshots artifacts/qa/screenshots
//
// Exits 0 when every invariant holds, 1 on a failed assertion, 2 on a setup
// problem. Screenshots go to a gitignored directory.

import { createRequire } from 'node:module';
import { existsSync, mkdirSync, readFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, '..', '..');

const PLAYWRIGHT_SEARCH_PATHS = [
  REPO,
  '/home/sedlock/.npm-global/lib/node_modules/@playwright/cli',
];

function loadPlaywright() {
  const tried = [];
  for (const base of PLAYWRIGHT_SEARCH_PATHS) {
    try {
      const entry = require.resolve('playwright', { paths: [base] });
      const pkg = require.resolve('playwright/package.json', { paths: [base] });
      return {
        playwright: require(entry),
        version: JSON.parse(readFileSync(pkg, 'utf8')).version,
        source: base,
      };
    } catch (error) {
      tried.push(`${base}: ${error.code || error.message}`);
    }
  }
  console.error('Playwright is not available. Searched:\n  ' + tried.join('\n  '));
  console.error('Install nothing new: run this on a host that already has it.');
  process.exit(2);
}

// --- arguments ---------------------------------------------------------------

function arg(name, fallback) {
  const index = process.argv.indexOf(`--${name}`);
  return index > -1 && process.argv[index + 1] ? process.argv[index + 1] : fallback;
}

const PAGE_DIR = resolve(arg('page', join(REPO, 'artifacts', 'qa', 'pages')));
const SHOT_DIR = resolve(arg('screenshots', join(REPO, 'artifacts', 'qa', 'screenshots')));
const KEEP_SHOTS = process.argv.includes('--screenshots-off') === false;

// Viewports chosen to bracket what the reader actually uses: an iPhone-class
// Outlook mobile pane, a larger phone, and a desktop Outlook/Mac reading pane.
const VIEWPORTS = [
  { name: 'outlook-mobile-390x844', width: 390, height: 844, mobile: true },
  { name: 'outlook-mobile-430x932', width: 430, height: 932, mobile: true },
  { name: 'outlook-desktop-1280x900', width: 1280, height: 900, mobile: false },
];

// --- assertions --------------------------------------------------------------

const failures = [];
const checks = [];

function check(name, condition, detail) {
  checks.push({ name, ok: Boolean(condition), detail });
  if (!condition) failures.push(`${name}${detail ? ` -- ${detail}` : ''}`);
}

function normalizeColor(value) {
  // getComputedStyle returns `rgb(r, g, b)`; compare on that form.
  const match = /^rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(value || '');
  if (!match) return value;
  return `rgb(${match[1]}, ${match[2]}, ${match[3]})`;
}

function hexToRgb(hex) {
  const value = hex.replace('#', '');
  const n = parseInt(value, 16);
  return `rgb(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255})`;
}

function relativeLuminance(rgb) {
  const match = /^rgba?\((\d+),\s*(\d+),\s*(\d+)/.exec(rgb || '');
  if (!match) return null;
  const channel = (value) => {
    const c = Number(value) / 255;
    return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
  };
  return (
    0.2126 * channel(match[1]) +
    0.7152 * channel(match[2]) +
    0.0722 * channel(match[3])
  );
}

// WCAG contrast, used to say "Standing is a different surface, not a dimmer
// one" as a number rather than as an opinion.
function contrastRatio(a, b) {
  const la = relativeLuminance(a);
  const lb = relativeLuminance(b);
  if (la === null || lb === null) return 0;
  const [hi, lo] = la > lb ? [la, lb] : [lb, la];
  return (hi + 0.05) / (lo + 0.05);
}

// --- main --------------------------------------------------------------------

const { playwright, version, source } = loadPlaywright();
const { chromium } = playwright;

const manifestPath = join(PAGE_DIR, 'manifest.json');
if (!existsSync(manifestPath)) {
  console.error(`No QA page at ${PAGE_DIR}. Build it first:`);
  console.error('  uv run python tools/qa/build_page.py --out ' + PAGE_DIR);
  process.exit(2);
}
const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
const pagePath = join(PAGE_DIR, manifest.html);
const expectedBody = hexToRgb(manifest.body_color);
const expectedAccent = hexToRgb(manifest.accent_color);

if (KEEP_SHOTS) mkdirSync(SHOT_DIR, { recursive: true });

const browser = await chromium.launch();
console.log(`playwright ${version} (from ${source})`);
console.log(`chromium ${browser.version()}`);
console.log(`page ${pagePath}`);
console.log('');

for (const viewport of VIEWPORTS) {
  const context = await browser.newContext({
    viewport: { width: viewport.width, height: viewport.height },
    deviceScaleFactor: viewport.mobile ? 2 : 1,
    isMobile: viewport.mobile,
    hasTouch: viewport.mobile,
  });
  const page = await context.newPage();
  await page.goto(pathToFileURL(pagePath).href, { waitUntil: 'load' });

  const scope = `[${viewport.name}]`;

  // --- colour: the 1 September regression --------------------------------
  const bodyColors = await page.evaluate(() => {
    const out = {};
    for (const card of document.querySelectorAll('.body-copy')) {
      // The card's own submission id is the id in its "View official
      // announcement" link, which every card carries.
      const row = card.closest('td');
      const link = row.querySelector('a[href*="SubmissionId="]');
      const id = link ? new URL(link.href).searchParams.get('SubmissionId') : null;
      if (!id) continue;
      // Classify by which of the digest's three roles a node inherits from.
      // A <span> inside an <h4> is heading text and a <span> inside an <a> is
      // link text; only everything else is body prose. Lumping them together
      // would make the assertion complain about the design working correctly.
      const role = (node) => {
        if (node.closest('h1,h2,h3,h4,h5,h6')) return 'heading';
        if (node.closest('a')) return 'link';
        return 'prose';
      };
      const measured = [...card.querySelectorAll('p,span,li,td,th,div')].map(
        (node) => ({ role: role(node), color: getComputedStyle(node).color }),
      );
      out[id] = {
        wrapper: getComputedStyle(card).color,
        prose: measured.filter((e) => e.role === 'prose').map((e) => e.color),
        headings: measured.filter((e) => e.role === 'heading').map((e) => e.color),
        links: [...card.querySelectorAll('a')].map(
          (node) => getComputedStyle(node).color,
        ),
        bold: [...card.querySelectorAll('strong,b')].map(
          (node) => getComputedStyle(node).fontWeight,
        ),
        italic: [...card.querySelectorAll('i,em')].map(
          (node) => getComputedStyle(node).fontStyle,
        ),
        underline: [...card.querySelectorAll('u')].map(
          (node) => getComputedStyle(node).textDecorationLine,
        ),
      };
    }
    return out;
  });

  const headlineColors = await page.evaluate(() =>
    [...document.querySelectorAll('a.subject-link')].map((node) => ({
      id: new URL(node.href).searchParams.get('SubmissionId'),
      color: getComputedStyle(node).color,
    })),
  );

  for (const card of manifest.cards) {
    const measured = bodyColors[card.submission_id];
    check(`${scope} card ${card.submission_id} has measurable body copy`, Boolean(measured));
    if (!measured) continue;
    check(
      `${scope} ${card.submission_id} body wrapper pins the digest ink`,
      normalizeColor(measured.wrapper) === expectedBody,
      normalizeColor(measured.wrapper),
    );

    const offending = measured.prose
      .map(normalizeColor)
      .filter((color) => color !== expectedBody);
    check(
      `${scope} ${card.submission_id} body prose is the digest ink`,
      measured.prose.length > 0 && offending.length === 0,
      offending.length ? `unexpected ${[...new Set(offending)].join(', ')}` : '',
    );

    const strayHeadings = measured.headings
      .map(normalizeColor)
      .filter((color) => color !== expectedAccent);
    check(
      `${scope} ${card.submission_id} body headings keep the accent colour`,
      strayHeadings.length === 0,
      [...new Set(strayHeadings)].join(', '),
    );

    const linkColors = [...new Set(measured.links.map(normalizeColor))];
    check(
      `${scope} ${card.submission_id} body links keep the digest link colour`,
      linkColors.every((color) => color === expectedAccent),
      linkColors.filter((c) => c !== expectedAccent).join(', '),
    );

    const headline = headlineColors.find((entry) => entry.id === card.submission_id);
    check(
      `${scope} ${card.submission_id} headline stays the accent colour`,
      headline && normalizeColor(headline.color) === expectedAccent,
      headline ? normalizeColor(headline.color) : 'headline not found',
    );
    check(
      `${scope} ${card.submission_id} headline and body prose differ`,
      headline && normalizeColor(headline.color) !== expectedBody,
    );

    // Emphasis must survive the colour policy.
    if (card.submission_id === '6612') {
      check(
        `${scope} ${card.submission_id} bold survives the colour policy`,
        measured.bold.some((weight) => Number(weight) >= 600),
        measured.bold.join(','),
      );
      check(
        `${scope} ${card.submission_id} italic survives the colour policy`,
        measured.italic.includes('italic'),
        measured.italic.join(','),
      );
      check(
        `${scope} ${card.submission_id} underline survives the colour policy`,
        measured.underline.some((line) => line.includes('underline')),
        measured.underline.join(','),
      );
    }
  }

  // --- alignment: the 9 September justification regression -----------------
  //
  // 6846 sets `text-align:justify` on every paragraph of its body. On a 390px
  // pane with no hyphenation that opens rivers of whitespace between words;
  // 6926, which carries no style attribute at all, always read correctly. The
  // render-time policy makes both compute the same way, so this asserts what
  // the paragraphs actually resolve to rather than what the markup says.
  const alignment = await page.evaluate(() => {
    const out = {};
    for (const card of document.querySelectorAll('.body-copy')) {
      const cell = card.closest('td');
      const link = cell.querySelector('a[href*="SubmissionId="]');
      const id = link ? new URL(link.href).searchParams.get('SubmissionId') : null;
      if (!id) continue;
      const blocks = [...card.querySelectorAll('p,div,li,blockquote,h1,h2,h3,h4,h5,h6')];
      out[id] = {
        wrapper: getComputedStyle(card).textAlign,
        blocks: blocks.map((node) => ({
          tag: node.tagName.toLowerCase(),
          align: getComputedStyle(node).textAlign,
          spacing: getComputedStyle(node).wordSpacing,
          inCell: Boolean(node.closest('td,th')),
        })),
        cells: [...card.querySelectorAll('td,th')].map(
          (node) => getComputedStyle(node).textAlign,
        ),
        meta: [...cell.querySelectorAll('.card-meta, .card-meta div')].map(
          (node) => getComputedStyle(node).textAlign,
        ),
      };
    }
    return out;
  });

  for (const card of manifest.cards) {
    const measured = alignment[card.submission_id];
    check(`${scope} ${card.submission_id} has measurable alignment`, Boolean(measured));
    if (!measured) continue;
    check(
      `${scope} ${card.submission_id} body wrapper is left-aligned`,
      measured.wrapper === 'left' || measured.wrapper === 'start',
      measured.wrapper,
    );
    // Justify never survives anywhere, in any card, at any viewport.
    const justified = measured.blocks.filter((b) => b.align === 'justify');
    check(
      `${scope} ${card.submission_id} no source justification survives`,
      justified.length === 0,
      justified.map((b) => b.tag).join(', '),
    );
    // Ordinary prose is left. A block inside a table cell is excluded: that is
    // the one place alignment is the author saying something about the data.
    const stray = measured.blocks.filter(
      (b) => !b.inCell && b.align !== 'left' && b.align !== 'start' && b.align !== 'center',
    );
    check(
      `${scope} ${card.submission_id} ordinary prose is left-aligned`,
      stray.length === 0,
      stray.map((b) => `${b.tag}=${b.align}`).join(', '),
    );
    // Word spacing exists only to support justification; none should be set.
    const spaced = measured.blocks.filter(
      (b) => b.spacing && b.spacing !== 'normal' && b.spacing !== '0px',
    );
    check(
      `${scope} ${card.submission_id} no justification word-spacing survives`,
      spaced.length === 0,
      spaced.map((b) => `${b.tag}=${b.spacing}`).join(', '),
    );
    const strayMeta = measured.meta.filter((a) => a !== 'left' && a !== 'start');
    check(
      `${scope} ${card.submission_id} source metadata prose is left-aligned`,
      strayMeta.length === 0,
      strayMeta.join(', '),
    );
  }

  const justifyCard = manifest.cards.find((c) => c.kind === 'justify-leak');
  const controlCard = manifest.cards.find((c) => c.kind === 'justify-control');
  check(
    `${scope} the justified regression case is on the page`,
    Boolean(justifyCard) && Boolean(alignment[justifyCard?.submission_id]),
  );
  if (justifyCard && controlCard) {
    const bad = alignment[justifyCard.submission_id];
    const good = alignment[controlCard.submission_id];
    check(
      `${scope} the regression case now matches its own control`,
      bad.blocks.length > 0 &&
        bad.blocks.every((b) => b.inCell || b.align === 'left' || b.align === 'start') &&
        good.blocks.every((b) => b.inCell || b.align === 'left' || b.align === 'start'),
      `bad=${[...new Set(bad.blocks.map((b) => b.align))].join('/')} ` +
        `control=${[...new Set(good.blocks.map((b) => b.align))].join('/')}`,
    );
  }

  // --- NEW -> STANDING: one barrier, and a persistent surface -------------
  const sections = await page.evaluate(() => {
    const transitions = [...document.querySelectorAll('.standing-transition')];
    const cardSurface = (selector) =>
      [...document.querySelectorAll(selector)].map((cell) => {
        const link = cell.querySelector('a[href*="SubmissionId="]');
        const badge = [...cell.querySelectorAll('td')].find((node) =>
          ['NEW', 'STANDING'].includes(node.textContent.trim()),
        );
        const body = cell.querySelector('.body-copy p, .body-copy');
        return {
          id: link ? new URL(link.href).searchParams.get('SubmissionId') : null,
          background: getComputedStyle(cell).backgroundColor,
          badge: badge ? badge.textContent.trim() : null,
          bodyColor: body ? getComputedStyle(body).color : null,
          right: Math.round(cell.getBoundingClientRect().right),
        };
      });
    return {
      transitionCount: transitions.length,
      transitionText: transitions.map((node) =>
        node.textContent.replace(/\s+/g, ' ').trim(),
      ),
      // Measured against the digest's own reading column, not the viewport: the
      // shell is a fixed 680px on desktop, so "full width" means the column.
      shellWidth: Math.round(
        (document.querySelector('table.shell') || document.body).getBoundingClientRect()
          .width,
      ),
      transitionBox: transitions.map((node) => {
        const box = node.getBoundingClientRect();
        return { width: Math.round(box.width), height: Math.round(box.height) };
      }),
      transitionBands: transitions.map((node) =>
        [...node.querySelectorAll('td')].map((cell) => ({
          background: getComputedStyle(cell).backgroundColor,
          height: Math.round(cell.getBoundingClientRect().height),
        })),
      ),
      newCards: cardSurface('td.card-new'),
      standingCards: cardSurface('td.card-standing'),
    };
  });

  check(
    `${scope} exactly one NEW -> STANDING barrier`,
    sections.transitionCount === 1,
    String(sections.transitionCount),
  );
  check(
    `${scope} the barrier keeps its section text`,
    sections.transitionText.some(
      (text) =>
        /STANDING/.test(text) &&
        new RegExp(`${manifest.counts.standing} continuing announcement`).test(text),
    ),
    sections.transitionText.join(' | '),
  );
  check(
    `${scope} the barrier spans the full reading column`,
    sections.transitionBox.every((box) => box.width >= sections.shellWidth - 1),
    sections.transitionBox.map((b) => `${b.width}x${b.height}`).join(', ') +
      ` in a ${sections.shellWidth}px column`,
  );
  // Breathing room is part of the design: the barrier is materially taller than
  // the 7px strip it replaced, so it cannot be scrolled past without noticing.
  check(
    `${scope} the barrier has real vertical presence`,
    sections.transitionBox.every((box) => box.height >= 40),
    sections.transitionBox.map((b) => `${b.height}px`).join(', '),
  );
  // A rule, a label and a caption: three distinct backgrounds, so the barrier
  // reads as a barrier rather than as one more grey strip.
  const bands = sections.transitionBands[0] || [];
  check(
    `${scope} the barrier is built from visually distinct bands`,
    new Set(bands.map((band) => normalizeColor(band.background))).size >= 3,
    bands.map((b) => `${normalizeColor(b.background)}@${b.height}px`).join(', '),
  );
  check(
    `${scope} the barrier carries the gold accent`,
    bands.some((band) => normalizeColor(band.background) === 'rgb(255, 204, 0)'),
    bands.map((b) => normalizeColor(b.background)).join(', '),
  );

  check(`${scope} the page has both New and Standing cards`,
    sections.newCards.length > 0 && sections.standingCards.length > 0,
    `${sections.newCards.length} new / ${sections.standingCards.length} standing`);

  const newSurfaces = new Set(sections.newCards.map((c) => normalizeColor(c.background)));
  const standingSurfaces = new Set(
    sections.standingCards.map((c) => normalizeColor(c.background)),
  );
  check(
    `${scope} New and Standing cards each have one consistent surface`,
    newSurfaces.size === 1 && standingSurfaces.size === 1,
    `${[...newSurfaces].join('/')} vs ${[...standingSurfaces].join('/')}`,
  );
  check(
    `${scope} the Standing surface measurably differs from the New surface`,
    [...standingSurfaces][0] !== [...newSurfaces][0],
    `${[...newSurfaces][0]} vs ${[...standingSurfaces][0]}`,
  );
  // Different, but not so different that Standing reads as disabled: the body
  // ink is identical on both and stays well clear of its own background.
  const proseColors = new Set(
    [...sections.newCards, ...sections.standingCards]
      .map((c) => normalizeColor(c.bodyColor))
      .filter(Boolean),
  );
  check(
    `${scope} Standing body copy keeps the same ink as New`,
    proseColors.size === 1 && [...proseColors][0] === expectedBody,
    [...proseColors].join(', '),
  );
  const standingContrast = contrastRatio(
    [...standingSurfaces][0],
    expectedBody,
  );
  check(
    `${scope} Standing body copy stays highly legible on its surface`,
    standingContrast >= 7,
    `contrast ${standingContrast.toFixed(1)}:1`,
  );
  check(
    `${scope} the badge on a Standing card still says STANDING`,
    sections.standingCards.every((card) => card.badge === 'STANDING'),
    sections.standingCards.map((c) => `${c.id}=${c.badge}`).join(', '),
  );
  check(
    `${scope} the badge on a New card still says NEW`,
    sections.newCards.every((card) => card.badge === 'NEW'),
    sections.newCards.map((c) => `${c.id}=${c.badge}`).join(', '),
  );
  const updatedCard = manifest.cards.find((c) => c.changed);
  if (updatedCard) {
    const updatedBadge = await page.evaluate((id) => {
      const link = [...document.querySelectorAll('a[href*="SubmissionId="]')].find(
        (node) => new URL(node.href).searchParams.get('SubmissionId') === id,
      );
      const cell = link ? link.closest('td.card') : null;
      if (!cell) return null;
      const badge = [...cell.querySelectorAll('td')].find(
        (node) => node.textContent.trim() === 'UPDATED',
      );
      if (!badge) return null;
      const style = getComputedStyle(badge);
      return { color: style.color, background: style.backgroundColor };
    }, updatedCard.submission_id);
    check(
      `${scope} an UPDATED Standing card still shows its UPDATED badge`,
      Boolean(updatedBadge),
      updatedCard.submission_id,
    );
    if (updatedBadge) {
      check(
        `${scope} the UPDATED badge keeps its gold treatment`,
        normalizeColor(updatedBadge.background) === 'rgb(255, 204, 0)',
        normalizeColor(updatedBadge.background),
      );
    }
  }
  const overflowingCards = [...sections.newCards, ...sections.standingCards].filter(
    (card) => card.right > viewport.width + 1,
  );
  check(
    `${scope} no card overflows the viewport`,
    overflowingCards.length === 0,
    overflowingCards.map((c) => `${c.id}@${c.right}`).join(', '),
  );

  // --- parking callout, unaffected by any of the above ---------------------
  const parkingBlocks = await page.evaluate(() =>
    [...document.querySelectorAll('td')]
      .filter((cell) => /^PARKING LOCATIONS?$/.test(
        (cell.querySelector('div')?.textContent || '').trim(),
      ))
      .map((cell) => ({
        background: getComputedStyle(cell).backgroundColor,
        align: getComputedStyle(cell).textAlign,
        right: Math.round(cell.getBoundingClientRect().right),
        link: cell.querySelector('a') ? getComputedStyle(cell.querySelector('a')).color : null,
      })),
  );
  check(
    `${scope} the parking callout is present and intact`,
    parkingBlocks.length === manifest.parking_callouts,
    `${parkingBlocks.length} of ${manifest.parking_callouts}`,
  );
  check(
    `${scope} the parking callout keeps its own surface and fits`,
    parkingBlocks.every(
      (block) =>
        normalizeColor(block.background) === 'rgb(250, 247, 242)' &&
        block.right <= viewport.width + 1 &&
        normalizeColor(block.link) === expectedAccent,
    ),
    parkingBlocks
      .map((b) => `${normalizeColor(b.background)}@${b.right} link=${normalizeColor(b.link)}`)
      .join(', '),
  );

  // --- multi-session calendar controls ------------------------------------
  const calendar = await page.evaluate(() => {
    const blocks = [];
    for (const sessions of document.querySelectorAll('.cal-sessions')) {
      blocks.push(sessions.textContent.trim());
    }
    // Grouped by card, because two *different* announcements may each offer a
    // single sitting and both say "Add to Calendar" -- that is correct, and a
    // flat uniqueness check would call it a defect.
    const cards = [...document.querySelectorAll('.body-copy')].map((body) => {
      const cell = body.closest('td');
      const link = cell.querySelector('a[href*="SubmissionId="]');
      return {
        id: link ? new URL(link.href).searchParams.get('SubmissionId') : null,
        labels: [...cell.querySelectorAll('a.cal-button')].map((node) =>
          node.textContent.replace(/\s+/g, ' ').trim(),
        ),
      };
    });
    return {
      sessionsLines: blocks,
      cards,
      buttons: [...document.querySelectorAll('a.cal-button')].map((node) => ({
        label: node.textContent.replace(/\s+/g, ' ').trim(),
        href: node.getAttribute('href'),
        width: node.getBoundingClientRect().width,
        height: node.getBoundingClientRect().height,
        right: node.getBoundingClientRect().right,
      })),
      whenLines: [...document.querySelectorAll('.cal-when')].map((node) =>
        node.textContent.trim(),
      ),
    };
  });

  check(
    `${scope} every offered session has its own calendar control`,
    calendar.buttons.length === manifest.calendar.session_actions,
    `${calendar.buttons.length} button(s) for ${manifest.calendar.session_actions} session(s)`,
  );
  check(
    `${scope} the multi-session card says how many sittings it offers`,
    calendar.sessionsLines.length === manifest.calendar.multi_session,
    calendar.sessionsLines.join(' | '),
  );
  const labels = calendar.buttons.map((button) => button.label);
  const ambiguous = calendar.cards.filter(
    (card) => new Set(card.labels).size !== card.labels.length,
  );
  check(
    `${scope} a card's session buttons are distinguishable from each other`,
    ambiguous.length === 0,
    ambiguous.map((card) => `${card.id}: ${card.labels.join(', ')}`).join(' | '),
  );
  const multi = calendar.cards.filter((card) => card.labels.length > 1);
  check(
    `${scope} every button on a multi-session card names its own date`,
    multi.every((card) => card.labels.every((label) => /\w+, \w+ \d/.test(label))),
    multi.map((card) => `${card.id}: ${card.labels.join(', ')}`).join(' | '),
  );
  check(
    `${scope} each session button targets a distinct time`,
    new Set(calendar.buttons.map((b) => b.href)).size === calendar.buttons.length,
  );
  check(
    `${scope} the Coffee Hours sittings are both offered`,
    labels.some((label) => label.includes('Sep 10')) &&
      labels.some((label) => label.includes('Sep 21')),
    labels.join(' | '),
  );
  check(
    `${scope} session times are shown beside their buttons`,
    calendar.whenLines.length >= manifest.calendar.session_actions,
    calendar.whenLines.join(' | '),
  );

  // A 44px tap target is Apple's own minimum; below it a thumb misses.
  const small = calendar.buttons.filter((button) => button.height < 40);
  check(
    `${scope} calendar buttons are a real tap target`,
    small.length === 0,
    small.map((b) => `${b.label}=${Math.round(b.height)}px`).join(', '),
  );
  const overflowing = calendar.buttons.filter(
    (button) => button.right > viewport.width + 1,
  );
  check(
    `${scope} calendar buttons fit the viewport`,
    overflowing.length === 0,
    overflowing.map((b) => b.label).join(', '),
  );

  // --- layout -------------------------------------------------------------
  const overflow = await page.evaluate(() => {
    const doc = document.documentElement;
    const widest = [...document.querySelectorAll('table,div,td,a,img,p')]
      .map((node) => ({
        tag: node.tagName,
        right: Math.round(node.getBoundingClientRect().right),
        cls: node.className || '',
      }))
      .filter((entry) => entry.right > window.innerWidth + 1)
      .slice(0, 5);
    return {
      scrollWidth: doc.scrollWidth,
      clientWidth: doc.clientWidth,
      widest,
    };
  });
  check(
    `${scope} no horizontal overflow`,
    overflow.scrollWidth <= overflow.clientWidth + 1,
    `scrollWidth=${overflow.scrollWidth} clientWidth=${overflow.clientWidth} ` +
      overflow.widest.map((w) => `${w.tag}.${w.cls}@${w.right}`).join(', '),
  );

  // --- badges stay legible ------------------------------------------------
  const badges = await page.evaluate(() => {
    const found = [];
    for (const cell of document.querySelectorAll('td')) {
      const text = cell.textContent.trim();
      if (!['NEW', 'STANDING', 'UPDATED', 'EMPLOYEE', 'STUDENT', 'EVERYONE'].includes(text))
        continue;
      const style = getComputedStyle(cell);
      const box = cell.getBoundingClientRect();
      found.push({
        text,
        color: style.color,
        background: style.backgroundColor,
        width: Math.round(box.width),
        height: Math.round(box.height),
      });
    }
    return found;
  });
  check(`${scope} status/audience badges are present`, badges.length > 0);
  const clipped = badges.filter((badge) => badge.height < 10 || badge.width < 20);
  check(
    `${scope} badges are not clipped`,
    clipped.length === 0,
    clipped.map((b) => `${b.text} ${b.width}x${b.height}`).join(', '),
  );
  const invisible = badges.filter(
    (badge) => normalizeColorNode(badge.color) === normalizeColorNode(badge.background),
  );
  check(
    `${scope} badge text is not the same colour as its own background`,
    invisible.length === 0,
    invisible.map((b) => b.text).join(', '),
  );

  // --- dark-mode approximation --------------------------------------------
  //
  // Outlook mobile force-inverts a light design. No browser reproduces its
  // exact transform, but the property that broke is reproducible: after a
  // uniform inversion, body prose and the headline must still be different
  // colours. Before the fix they inverted to the same peach.
  await page.addStyleTag({
    content: 'html { filter: invert(1) hue-rotate(180deg); }',
  });
  const inverted = await page.evaluate(() => {
    const card = document.querySelector('.body-copy');
    const headline = document.querySelector('a.subject-link');
    const surface = (selector) => {
      const cell = document.querySelector(selector);
      return cell ? getComputedStyle(cell).backgroundColor : null;
    };
    const prose = [...document.querySelectorAll('.body-copy p')].map((node) =>
      getComputedStyle(node).textAlign,
    );
    const bands = [...document.querySelectorAll('.standing-transition td')].map(
      (cell) => getComputedStyle(cell).backgroundColor,
    );
    return {
      body: getComputedStyle(card.querySelector('p') || card).color,
      headline: getComputedStyle(headline).color,
      newSurface: surface('td.card-new'),
      standingSurface: surface('td.card-standing'),
      standingInk: (() => {
        const node = document.querySelector('td.card-standing .body-copy p');
        return node ? getComputedStyle(node).color : null;
      })(),
      newInk: (() => {
        const node = document.querySelector('td.card-new .body-copy p');
        return node ? getComputedStyle(node).color : null;
      })(),
      prose,
      bands,
    };
  });
  check(
    `${scope} dark-mode approximation keeps prose and headline distinct`,
    normalizeColor(inverted.body) !== normalizeColor(inverted.headline),
    `${inverted.body} vs ${inverted.headline}`,
  );
  // The reader's primary context is Outlook mobile's dark reader mode. No
  // browser reproduces its exact transform -- this is regression QA, not proof
  // of identical rendering -- but the properties that must survive a uniform
  // inversion are all checkable: the section surfaces stay different, the two
  // sections keep the same ink, that ink stays legible, and the barrier keeps
  // its bands.
  check(
    `${scope} dark-mode approximation keeps New and Standing surfaces apart`,
    Boolean(inverted.newSurface) &&
      normalizeColor(inverted.newSurface) !== normalizeColor(inverted.standingSurface),
    `${normalizeColor(inverted.newSurface)} vs ${normalizeColor(inverted.standingSurface)}`,
  );
  check(
    `${scope} dark-mode approximation keeps Standing body ink equal to New`,
    normalizeColor(inverted.standingInk) === normalizeColor(inverted.newInk),
    `${normalizeColor(inverted.standingInk)} vs ${normalizeColor(inverted.newInk)}`,
  );
  const invertedContrast = contrastRatio(inverted.standingSurface, inverted.standingInk);
  check(
    `${scope} dark-mode approximation keeps Standing prose legible`,
    invertedContrast >= 7,
    `contrast ${invertedContrast.toFixed(1)}:1`,
  );
  check(
    `${scope} dark-mode approximation keeps the barrier's bands distinct`,
    new Set(inverted.bands.map(normalizeColor)).size >= 3,
    inverted.bands.map(normalizeColor).join(', '),
  );
  check(
    `${scope} dark-mode approximation keeps prose left-aligned`,
    inverted.prose.length > 0 &&
      inverted.prose.every((value) => value === 'left' || value === 'start'),
    [...new Set(inverted.prose)].join(', '),
  );

  // Viewport-only by default: a full-page shot of a real digest runs to tens of
  // megabytes and nothing here reads below the fold. `--full-page` when a human
  // wants to scroll one.
  const fullPage = process.argv.includes('--full-page');
  if (KEEP_SHOTS) {
    await page.screenshot({
      path: join(SHOT_DIR, `${viewport.name}-dark.png`),
      fullPage,
    });
  }
  await page.addStyleTag({ content: 'html { filter: none; }' });
  if (KEEP_SHOTS) {
    await page.screenshot({
      path: join(SHOT_DIR, `${viewport.name}-light.png`),
      fullPage,
    });
  }

  await context.close();
}

await browser.close();

function normalizeColorNode(value) {
  return normalizeColor(value);
}

const passed = checks.filter((entry) => entry.ok).length;
console.log(`${passed}/${checks.length} invariant(s) held`);
if (failures.length) {
  console.error('\nFAILED:');
  for (const failure of failures) console.error(`  ${failure}`);
  process.exit(1);
}
if (KEEP_SHOTS) console.log(`screenshots: ${SHOT_DIR}`);
console.log('VISUAL QA OK');
