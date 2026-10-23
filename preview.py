#!/usr/bin/env python3
"""Preview a tree of markdown files in the browser with GitHub styling and Mermaid.

Serves files under a root directory on localhost. A request for a .md file returns a page that fetches the raw
markdown and renders it client-side with marked, github-markdown-css, highlight.js, and mermaid. A sidebar shows the
markdown files under the root as a tree. The page polls the server and re-renders when the file changes.

With --feedback, the page also lets you comment on selected text or whole blocks and submit the comments in batches.
An agent picks a batch up with `preview.py feedback wait`, edits the files, and answers with `preview.py feedback reply`.
Comments live in a sqlite database per served root.
"""

import argparse
import contextlib
import hashlib
import html
import json
import os
import secrets
import shlex
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

MARKDOWN_SUFFIXES = (".md", ".markdown")
SKIP_DIRS = {"node_modules", "vendor", "__pycache__", "dist", "build", "target"}
MAX_TREE_FILES = 20000
INDEX_NAMES = ["readme", "index"]
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "gh-markdown-preview"
PREFS_PATH = CONFIG_DIR / "prefs.json"
FEEDBACK_DIR = CONFIG_DIR / "feedback"
# Open folders and sidebar scroll only make sense for the tree they were saved against. Everything else is global.
ROOT_PREFS = {"gmp.open", "gmp.scroll"}
PREFS_LOCK = threading.Lock()
# A waiting agent refreshes its heartbeat every HEARTBEAT_EVERY seconds. The page treats an older one as gone.
HEARTBEAT_EVERY = 5
HEARTBEAT_FRESH = 15
SCRIPT = Path(__file__).resolve()

FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY,
  file TEXT NOT NULL,
  quote TEXT NOT NULL,
  prefix TEXT NOT NULL,
  suffix TEXT NOT NULL,
  line_start INTEGER NOT NULL,
  line_end INTEGER NOT NULL,
  context TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (
  id INTEGER PRIMARY KEY,
  submitted_at REAL NOT NULL,
  claimed_at REAL,
  done_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY,
  thread_id INTEGER NOT NULL REFERENCES threads(id) ON DELETE CASCADE,
  author TEXT NOT NULL,
  body TEXT NOT NULL,
  batch_id INTEGER REFERENCES batches(id),
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS threads_file ON threads (file);
CREATE INDEX IF NOT EXISTS messages_thread ON messages (thread_id);
"""

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="icon" href="data:,">
<link id="md-light" rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5.5.1/github-markdown-light.min.css">
<link id="md-dark" rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5.5.1/github-markdown-dark.min.css">
<link id="hl-light" rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/styles/github.min.css">
<link id="hl-dark" rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/styles/github-dark.min.css">
<script>
  // Runs before first paint so a stored theme or width never flashes the default.
  window.gmpApplyTheme = function (mode) {
    const media = {
      auto: ['(prefers-color-scheme: light)', '(prefers-color-scheme: dark)'],
      light: ['all', 'not all'],
      dark: ['not all', 'all'],
    }[mode];
    for (const kind of ['md', 'hl']) {
      document.getElementById(kind + '-light').media = media[0];
      document.getElementById(kind + '-dark').media = media[1];
    }
    document.documentElement.dataset.theme = mode;
  };
  // Preferences live on the server, because each run gets a new port and localStorage is per origin.
  window.gmpPrefs = __PREFS__;
  const gmpTheme = window.gmpPrefs['gmp.theme'];
  window.gmpApplyTheme(['auto', 'light', 'dark'].includes(gmpTheme) ? gmpTheme : 'auto');
  if (window.gmpPrefs['gmp.wide']) document.documentElement.classList.add('wide');
</script>
<style>
  :root {
    color-scheme: light;
    --bg: #ffffff; --side-bg: #f6f8fa; --border: #d0d7de; --fg: #1f2328; --muted: #59636e;
    --hover: #eaeef2; --current: #ddf4ff; --accent: #0969da;
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --bg: #0d1117; --side-bg: #010409; --border: #30363d; --fg: #e6edf3; --muted: #9198a1;
    --hover: #161b22; --current: #121d2f; --accent: #4493f8;
  }
  @media (prefers-color-scheme: dark) {
    :root[data-theme="auto"] {
      color-scheme: dark;
      --bg: #0d1117; --side-bg: #010409; --border: #30363d; --fg: #e6edf3; --muted: #9198a1;
      --hover: #161b22; --current: #121d2f; --accent: #4493f8;
    }
  }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--fg); }
  body { display: flex; font: 14px -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif; }
  #sidebar {
    width: var(--side-width, 300px); min-width: 180px; max-width: 60vw; flex: none; height: 100vh;
    position: sticky; top: 0; display: flex; flex-direction: column;
    background: var(--side-bg); border-right: 1px solid var(--border); resize: horizontal; overflow: hidden;
  }
  body.collapsed #sidebar { display: none; }
  #sidebar header { padding: 12px 12px 8px; border-bottom: 1px solid var(--border); }
  #sidebar header a { color: var(--fg); font-weight: 600; text-decoration: none; display: block; margin-right: 28px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #filter {
    box-sizing: border-box; width: 100%; margin-top: 8px; padding: 5px 8px; font: inherit; color: var(--fg);
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
  }
  #tree { overflow: auto; flex: 1; padding: 6px 0 24px; }
  #tree ul { list-style: none; margin: 0; padding: 0; }
  #tree details > ul { margin-left: 13px; padding-left: 6px; border-left: 1px solid var(--border); }
  #tree summary, #tree a.file {
    display: block; padding: 2px 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    border-radius: 6px; margin: 0 6px; cursor: pointer;
  }
  #tree summary { color: var(--fg); list-style: none; }
  #tree summary::-webkit-details-marker { display: none; }
  #tree summary::before { content: "\\25B8"; display: inline-block; width: 14px; color: var(--muted); }
  #tree details[open] > summary::before { content: "\\25BE"; }
  #tree a.file { color: var(--fg); text-decoration: none; padding-left: 26px; }
  #tree summary:hover, #tree a.file:hover { background: var(--hover); }
  #tree a.file.current { background: var(--current); color: var(--accent); font-weight: 600; }
  #tree .empty { padding: 4px 18px; color: var(--muted); }
  main { flex: 1; min-width: 0; }
  #toggle, #tools button {
    padding: 2px 7px; font-size: 12px; cursor: pointer;
    color: var(--muted); background: var(--side-bg); border: 1px solid var(--border); border-radius: 6px;
  }
  #toggle:hover, #tools button:hover { color: var(--fg); }
  #toggle { position: fixed; left: 8px; top: 8px; z-index: 1; }
  #tools { position: fixed; right: 16px; top: 8px; z-index: 1; display: flex; gap: 6px; }
  #tools button[aria-pressed="true"] { color: var(--accent); border-color: var(--accent); }
  body:not(.collapsed) #toggle { left: calc(var(--side-px, 300px) - 30px); }
  .markdown-body { box-sizing: border-box; max-width: 1012px; margin: 0 auto; padding: 40px 45px 32px; }
  html.wide .markdown-body { max-width: none; }
  @media (max-width: 767px) { .markdown-body { padding: 16px; } #sidebar { display: none; } }
  .crumbs { font-size: 12px; margin-bottom: 16px; font-family: ui-monospace, monospace; color: var(--muted); }
  .mermaid { text-align: center; margin-bottom: 16px; }
  .mermaid-error { color: #cf222e; white-space: pre-wrap; }
</style>
</head>
<body>
<nav id="sidebar">
  <header>
    <a href="/">__ROOT__</a>
    <input id="filter" type="search" placeholder="Filter files" autocomplete="off">
  </header>
  <div id="tree"></div>
</nav>
<button id="toggle" title="Toggle sidebar (\\)">&#9776;</button>
<div id="tools">
  <button id="theme" title="Cycle theme: auto, light, dark (t)"></button>
  <button id="wide" title="Toggle wide mode (w)">&#8596; Wide</button>
</div>
<main>
  <article class="markdown-body">
    <div class="crumbs">__CRUMBS__</div>
    <div id="content">Loading…</div>
  </article>
</main>
<script type="module">
import { marked } from 'https://cdn.jsdelivr.net/npm/marked@12.0.2/lib/marked.esm.js';
import { gfmHeadingId } from 'https://cdn.jsdelivr.net/npm/marked-gfm-heading-id@3.1.3/+esm';
import hljs from 'https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/es/highlight.min.js';
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs';

const path = __PATH__;
const kind = __KIND__;
const feedbackConfig = __FEEDBACK__;
marked.use(gfmHeadingId());

const store = {
  pending: {},
  timer: null,
  get(key, fallback) {
    return window.gmpPrefs[key] ?? fallback;
  },
  set(key, value) {
    window.gmpPrefs[key] = value;
    this.pending[key] = value;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.flush(), 300);
  },
  flush() {
    clearTimeout(this.timer);
    if (!Object.keys(this.pending).length) return;
    const body = JSON.stringify(this.pending);
    this.pending = {};
    navigator.sendBeacon('/__prefs', new Blob([body], { type: 'application/json' }));
  },
};
window.addEventListener('pagehide', () => store.flush());

const feedback = feedbackConfig ? await import('/__feedback.js') : null;
feedback?.setup({ marked, path, kind, store, token: feedbackConfig.token });

// Sidebar.

const openDirs = new Set(store.get('gmp.open', []));
let tree = null;

function ancestors(p) {
  const parts = p.split('/').filter(Boolean);
  return parts.slice(0, kind === 'dir' ? parts.length : -1).map((_, i) => '/' + parts.slice(0, i + 1).join('/') + '/');
}

function matches(node, q) {
  if (!q) return true;
  if (node.path.toLowerCase().includes(q)) return true;
  return (node.children || []).some((c) => matches(c, q));
}

function renderNode(node, q) {
  const li = document.createElement('li');
  if (node.children) {
    const details = document.createElement('details');
    details.open = q ? true : openDirs.has(node.path);
    details.addEventListener('toggle', () => {
      if (q) return;
      details.open ? openDirs.add(node.path) : openDirs.delete(node.path);
      store.set('gmp.open', [...openDirs]);
    });
    const summary = document.createElement('summary');
    summary.textContent = node.name;
    summary.title = node.path;
    details.append(summary, renderList(node.children, q));
    li.append(details);
  } else {
    const a = document.createElement('a');
    a.className = 'file' + (node.path === path ? ' current' : '');
    a.href = node.path;
    a.textContent = node.name;
    a.title = node.path;
    li.append(a);
  }
  return li;
}

function renderList(nodes, q) {
  const ul = document.createElement('ul');
  for (const node of nodes) if (matches(node, q)) ul.append(renderNode(node, q));
  return ul;
}

function renderTree() {
  const q = document.getElementById('filter').value.trim().toLowerCase();
  const el = document.getElementById('tree');
  el.replaceChildren(renderList(tree.children, q));
  if (!el.querySelector('li')) el.innerHTML = '<div class="empty">No matching files</div>';
}

async function loadTree() {
  const res = await fetch('/__tree', { cache: 'no-store' });
  tree = await res.json();
  for (const dir of ancestors(path)) openDirs.add(dir);
  store.set('gmp.open', [...openDirs]);
  renderTree();
  const sidebar = document.getElementById('tree');
  sidebar.scrollTop = store.get('gmp.scroll', 0);
  const current = sidebar.querySelector('a.current');
  if (current) {
    const box = current.getBoundingClientRect();
    const view = sidebar.getBoundingClientRect();
    if (box.top < view.top || box.bottom > view.bottom) current.scrollIntoView({ block: 'center' });
  }
  sidebar.addEventListener('scroll', () => store.set('gmp.scroll', sidebar.scrollTop));
}

document.getElementById('filter').addEventListener('input', renderTree);
document.getElementById('filter').addEventListener('keydown', (e) => {
  if (e.key === 'Enter') document.querySelector('#tree a.file')?.click();
  if (e.key === 'Escape') { e.target.value = ''; renderTree(); }
});

function setCollapsed(collapsed) {
  document.body.classList.toggle('collapsed', collapsed);
  store.set('gmp.collapsed', collapsed);
}
setCollapsed(store.get('gmp.collapsed', false));
document.getElementById('toggle').addEventListener('click', () => setCollapsed(!document.body.classList.contains('collapsed')));
// Theme and width.

const themes = ['auto', 'light', 'dark'];
const themeLabels = { auto: '\u25D0 Auto', light: '\u2600 Light', dark: '\u263E Dark' };
const systemDark = window.matchMedia('(prefers-color-scheme: dark)');
const isDark = () => {
  const mode = document.documentElement.dataset.theme;
  return mode === 'dark' || (mode === 'auto' && systemDark.matches);
};

function setTheme(mode) {
  window.gmpApplyTheme(mode);
  store.set('gmp.theme', mode);
  document.getElementById('theme').textContent = themeLabels[mode];
}
setTheme(document.documentElement.dataset.theme);
const cycleTheme = () => {
  const next = themes[(themes.indexOf(document.documentElement.dataset.theme) + 1) % themes.length];
  const before = isDark();
  setTheme(next);
  if (isDark() !== before) render();
};
document.getElementById('theme').addEventListener('click', cycleTheme);
systemDark.addEventListener('change', () => {
  if (document.documentElement.dataset.theme === 'auto') render();
});

function setWide(wide) {
  document.documentElement.classList.toggle('wide', wide);
  document.getElementById('wide').setAttribute('aria-pressed', String(wide));
  store.set('gmp.wide', wide);
}
setWide(document.documentElement.classList.contains('wide'));
const toggleWide = () => setWide(!document.documentElement.classList.contains('wide'));
document.getElementById('wide').addEventListener('click', toggleWide);

document.addEventListener('keydown', (e) => {
  if (['INPUT', 'TEXTAREA'].includes(e.target.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.key === 't') cycleTheme();
  if (e.key === 'w') toggleWide();
  if (e.key === '\\\\') setCollapsed(!document.body.classList.contains('collapsed'));
  if (e.key === '/') { e.preventDefault(); setCollapsed(false); document.getElementById('filter').focus(); }
});

const sidebarEl = document.getElementById('sidebar');
const width = store.get('gmp.width', null);
if (width) sidebarEl.style.width = width + 'px';
new ResizeObserver(() => {
  if (!sidebarEl.offsetWidth) return;
  store.set('gmp.width', sidebarEl.offsetWidth);
  document.body.style.setProperty('--side-px', sidebarEl.offsetWidth + 'px');
}).observe(sidebarEl);

// Content.

function findDir(p) {
  let node = tree;
  for (const name of p.split('/').filter(Boolean)) {
    node = (node.children || []).find((c) => c.children && c.name === name);
    if (!node) return null;
  }
  return node;
}

async function render() {
  const content = document.getElementById('content');
  if (kind === 'dir') {
    const node = findDir(path);
    const items = (node?.children || []).map((c) => `<li><a href="${c.path}">${c.name}${c.children ? '/' : ''}</a></li>`);
    content.innerHTML = items.length ? `<ul>${items.join('')}</ul>` : '<p>No markdown files here.</p>';
    return;
  }

  const res = await fetch(path + '?raw=1', { cache: 'no-store' });
  const md = await res.text();
  const y = window.scrollY;
  content.innerHTML = marked.parse(md);
  mermaid.initialize({ startOnLoad: false, theme: isDark() ? 'dark' : 'default' });

  content.querySelectorAll('pre > code.language-mermaid').forEach((code) => {
    const div = document.createElement('div');
    div.className = 'mermaid';
    div.textContent = code.textContent;
    code.parentElement.replaceWith(div);
  });
  content.querySelectorAll('pre > code').forEach((code) => hljs.highlightElement(code));

  for (const [i, div] of [...content.querySelectorAll('.mermaid')].entries()) {
    try {
      const { svg } = await mermaid.render('mermaid-' + i + '-' + Date.now(), div.textContent);
      div.innerHTML = svg;
    } catch (err) {
      div.className = 'mermaid-error';
      div.textContent = 'Mermaid error: ' + err.message + '\\n\\n' + div.textContent;
    }
  }
  feedback?.afterRender(content, md);
  window.scrollTo(0, y);
}

let mtime = null;
async function poll() {
  try {
    const res = await fetch('/__mtime?path=' + encodeURIComponent(path), { cache: 'no-store' });
    const { mtime: next } = await res.json();
    if (mtime !== null && next !== mtime) {
      const y = window.scrollY;
      if (kind === 'dir') await loadTree();
      await render();
      window.scrollTo(0, y);
    }
    mtime = next;
  } catch (_) {
    // Server stopped. Keep the last render.
  }
  setTimeout(poll, 1000);
}

await loadTree();
await render();
if (location.hash) document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView();
poll();
</script>
</body>
</html>
"""

# Served as /__feedback.js and imported by the page only when --feedback is on.
FEEDBACK_JS = r"""
const MARKER = 'gmp-src:';
const PANEL_WIDTH = 380;

let ctx;
let content = null;
let sourceLines = [];
let index = null;
let state = { threads: [], drafts: 0, agent: {} };
let stateJSON = '';
let anchors = new Map();
let activeId = null;
let showResolved = false;
let panelDirty = false;
const replyText = new Map();
const ui = {};

export function setup(options) {
  ctx = options;
  ctx.marked.use({ hooks: { processAllTokens: markSourceLines } });
  injectStyle();
  buildUI();
  setPanel(ctx.store.get('gmp.panel', false));
  poll();
}

// Called after every render with the content element and the markdown it came from.
export function afterRender(el, md) {
  content = el;
  sourceLines = md.replace(/\r\n?/g, '\n').split('\n');
  tagBlocks();
  index = null;
  anchorAll();
}

// Source lines.
//
// Each top-level token gets an HTML comment ahead of it that names its source lines. After rendering, tagBlocks copies
// the range onto the elements that follow each comment as data-src, so any rendered element maps back to the lines
// that produced it.

function markSourceLines(tokens) {
  const out = [];
  let line = 1;
  for (const token of tokens) {
    const raw = token.raw || '';
    if (token.type !== 'space' && token.type !== 'def' && raw.trim()) {
      const end = line + raw.replace(/\n+$/, '').split('\n').length - 1;
      out.push({ type: 'html', block: true, pre: false, raw: '', text: `<!--${MARKER}${line}:${end}-->` });
    }
    out.push(token);
    line += (raw.match(/\n/g) || []).length;
  }
  out.links = tokens.links;
  return out;
}

function tagBlocks() {
  const walker = document.createTreeWalker(content, NodeFilter.SHOW_COMMENT);
  const markers = [];
  while (walker.nextNode()) {
    if (walker.currentNode.data.startsWith(MARKER)) markers.push(walker.currentNode);
  }
  for (const marker of markers) {
    const [start, end] = marker.data.slice(MARKER.length).split(':');
    for (let n = marker.nextSibling; n && !isMarker(n); n = n.nextSibling) {
      if (n.nodeType === Node.ELEMENT_NODE) n.dataset.src = `${start}-${end}`;
    }
  }
}

const isMarker = (n) => n.nodeType === Node.COMMENT_NODE && n.data.startsWith(MARKER);

function blockOf(node) {
  const el = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
  return el?.closest('[data-src]') || null;
}

function linesOf(el) {
  const [start, end] = (el?.dataset.src || '0-0').split('-').map(Number);
  return { line_start: start, line_end: end };
}

function sourceOf(lineStart, lineEnd) {
  return lineStart ? sourceLines.slice(lineStart - 1, lineEnd).join('\n') : '';
}

// Text positions.
//
// Anchors are offsets into the concatenated text of the rendered page, skipping Mermaid diagrams, whose SVG text
// changes from one render to the next.

function textIndex() {
  if (index) return index;
  const nodes = [];
  let text = '';
  const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement.closest('.mermaid, svg, style, script') ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  while (walker.nextNode()) {
    nodes.push({ node: walker.currentNode, start: text.length });
    text += walker.currentNode.data;
  }
  return (index = { nodes, text });
}

function pointOffset(container, offset) {
  const range = document.createRange();
  range.setStart(content, 0);
  range.setEnd(container, offset);
  let total = 0;
  for (const { node } of textIndex().nodes) {
    if (node === container) return total + offset;
    if (!range.intersectsNode(node)) break;
    total += node.data.length;
  }
  return total;
}

// Returns the text node and offset of the character at off.
function charPoint(off) {
  for (const { node, start } of textIndex().nodes) {
    if (off < start + node.data.length) return [node, off - start];
  }
  return null;
}

function rangeFor(start, end) {
  const first = charPoint(start);
  const last = charPoint(end - 1);
  if (!first || !last) return null;
  const range = document.createRange();
  range.setStart(first[0], first[1]);
  range.setEnd(last[0], last[1] + 1);
  return range;
}

function captureSelection() {
  if (!content) return null;
  const sel = getSelection();
  if (!sel.rangeCount || sel.isCollapsed) return null;
  const range = sel.getRangeAt(0);
  if (!content.contains(range.commonAncestorContainer)) return null;
  const start = pointOffset(range.startContainer, range.startOffset);
  const end = pointOffset(range.endContainer, range.endOffset);
  const { text } = textIndex();
  const quote = text.slice(start, end);
  if (!quote.trim()) return null;
  const first = linesOf(blockOf(charPoint(start)[0]));
  const last = linesOf(blockOf(charPoint(end - 1)[0]));
  const lineStart = Math.min(first.line_start, last.line_start);
  const lineEnd = Math.max(first.line_end, last.line_end);
  const rects = range.getClientRects();
  return {
    quote,
    prefix: text.slice(Math.max(0, start - 32), start),
    suffix: text.slice(end, end + 32),
    line_start: lineStart,
    line_end: lineEnd,
    context: sourceOf(lineStart, lineEnd),
    rect: rects[rects.length - 1] || range.getBoundingClientRect(),
    range: range.cloneRange(),
  };
}

function captureBlock(el) {
  const lines = linesOf(el);
  return { quote: '', prefix: '', suffix: '', ...lines, context: sourceOf(lines.line_start, lines.line_end), el };
}

// Anchoring.
//
// A text comment re-anchors to the occurrence of its quote whose surrounding text best matches what surrounded it when
// it was made. A block comment re-anchors to the block whose source is unchanged. A comment that finds neither is
// outdated: it stays in the panel but has nothing to highlight.

function anchorAll() {
  anchors = new Map();
  if (content) for (const t of state.threads) anchors.set(t.id, locate(t));
  paint();
}

function locate(t) {
  if (!t.quote) {
    const el = [...content.querySelectorAll('[data-src]')].find((e) => {
      const lines = linesOf(e);
      return sourceOf(lines.line_start, lines.line_end) === t.context;
    });
    return el ? { el } : null;
  }
  const { text } = textIndex();
  let best = -1;
  let bestScore = -1;
  for (let i = text.indexOf(t.quote); i !== -1; i = text.indexOf(t.quote, i + 1)) {
    const before = text.slice(Math.max(0, i - t.prefix.length), i);
    const after = text.slice(i + t.quote.length, i + t.quote.length + t.suffix.length);
    const score = sharedSuffix(before, t.prefix) + sharedPrefix(after, t.suffix);
    if (score > bestScore) [best, bestScore] = [i, score];
  }
  if (best < 0) return null;
  const range = rangeFor(best, best + t.quote.length);
  return range ? { range, el: blockOf(range.startContainer) } : null;
}

function sharedPrefix(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[n] === b[n]) n++;
  return n;
}

function sharedSuffix(a, b) {
  let n = 0;
  while (n < a.length && n < b.length && a[a.length - 1 - n] === b[b.length - 1 - n]) n++;
  return n;
}

const highlighted = (t) => t.status === 'open' || t.id === activeId;

function paint() {
  document.querySelectorAll('.gmp-block').forEach((e) => e.classList.remove('gmp-block', 'gmp-block-active'));
  const normal = [];
  const active = [];
  for (const t of state.threads) {
    const a = anchors.get(t.id);
    if (!a || !highlighted(t)) continue;
    if (a.range) {
      (t.id === activeId ? active : normal).push(a.range);
    } else {
      a.el.classList.add('gmp-block');
      if (t.id === activeId) a.el.classList.add('gmp-block-active');
    }
  }
  if (window.CSS?.highlights && window.Highlight) {
    CSS.highlights.set('gmp-comment', new Highlight(...normal));
    CSS.highlights.set('gmp-active', new Highlight(...active));
  }
}

function threadAt(e) {
  if (e.target.closest('a')) return null;
  const hit = (r) => e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
  for (const t of state.threads) {
    const a = anchors.get(t.id);
    if (!a || !highlighted(t)) continue;
    if (a.range ? [...a.range.getClientRects()].some(hit) : a.el.contains(e.target)) return t;
  }
  return null;
}

// Server.

async function api(route, body) {
  const res = await fetch('/__feedback/' + route, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-GMP-Token': ctx.token },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) {
    alertError(await res.text());
    return null;
  }
  const out = await res.json();
  await refresh();
  return out;
}

async function refresh() {
  try {
    const res = await fetch('/__feedback/state?path=' + encodeURIComponent(ctx.path), {
      cache: 'no-store',
      headers: { 'X-GMP-Token': ctx.token },
    });
    if (!res.ok) return;
    const next = await res.text();
    if (next === stateJSON) return;
    stateJSON = next;
    state = JSON.parse(next);
    anchorAll();
    renderPanel();
    renderWidget();
  } catch (_) {
    // Server stopped. Keep what's on screen.
  }
}

async function poll() {
  await refresh();
  setTimeout(poll, 1000);
}

function alertError(text) {
  ui.widget.hidden = false;
  ui.widget.replaceChildren(h('span', { class: 'gmp-error' }, 'Feedback error: ' + text));
}

// UI.

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v !== false && v != null) el.setAttribute(k, v === true ? '' : v);
  }
  el.append(...children.flat().filter((c) => c != null && c !== false));
  return el;
}

// Renders message text with `code` spans and nothing else.
function formatBody(text) {
  return text.split(/(`[^`\n]+`)/).map((part) => (/^`[^`]+`$/.test(part) ? h('code', {}, part.slice(1, -1)) : part));
}

function buildUI() {
  ui.toolsButton = h('button', { id: 'gmp-comments', title: 'Comments (c)', onclick: () => setPanel(!document.body.classList.contains('gmp-panel-open')) });
  document.getElementById('tools').prepend(ui.toolsButton);

  ui.addButton = h('button', { class: 'gmp-add', title: 'Comment on this block', hidden: true, onmousedown: (e) => e.preventDefault(), onclick: () => {
    if (ui.addButton.block) openComposer(captureBlock(ui.addButton.block));
  } }, '+');
  ui.selButton = h('button', { class: 'gmp-sel', hidden: true, onmousedown: (e) => e.preventDefault(), onclick: () => {
    if (ui.pending) openComposer(ui.pending);
  } }, 'Comment');
  // The composer lives in the panel rather than floating over the page, so writing a comment narrows the content
  // instead of covering it. That matters in wide mode, where text runs to the edge of the window.
  ui.panelHead = h('div');
  ui.composer = h('div', { class: 'gmp-composer', hidden: true });
  ui.cards = h('div');
  ui.panel = h('aside', { id: 'gmp-panel' }, ui.panelHead, ui.composer, ui.cards);
  ui.widget = h('div', { id: 'gmp-widget', hidden: true });
  document.body.append(ui.addButton, ui.selButton, ui.panel, ui.widget);

  const article = document.querySelector('.markdown-body');
  article.addEventListener('mousemove', (e) => placeAddButton(article, e));
  article.addEventListener('mouseleave', (e) => {
    if (e.relatedTarget !== ui.addButton) ui.addButton.hidden = true;
  });
  ui.addButton.addEventListener('mouseleave', (e) => {
    if (!article.contains(e.relatedTarget)) ui.addButton.hidden = true;
  });

  document.addEventListener('mouseup', (e) => {
    if (ui.panel.contains(e.target) || e.target === ui.selButton) return;
    setTimeout(() => {
      ui.pending = captureSelection();
      if (!ui.pending) return (ui.selButton.hidden = true);
      ui.selButton.hidden = false;
      placeAt(ui.selButton, ui.pending.rect);
    });
  });
  document.addEventListener('selectionchange', () => {
    if (getSelection().isCollapsed) ui.selButton.hidden = true;
  });

  document.getElementById('content').addEventListener('click', (e) => {
    if (!getSelection().isCollapsed) return;
    const t = threadAt(e);
    if (t) focusThread(t.id, false);
  });

  document.addEventListener('keydown', (e) => {
    if (['INPUT', 'TEXTAREA'].includes(e.target.tagName) || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key !== 'c') return;
    e.preventDefault();
    const sel = captureSelection();
    sel ? openComposer(sel) : setPanel(!document.body.classList.contains('gmp-panel-open'));
  });

  ui.panel.addEventListener('focusout', () => setTimeout(() => {
    if (panelDirty && !ui.cards.contains(document.activeElement)) renderPanel();
  }));
}

// Places el below rect, kept clear of the window's right edge and of the panel. el must be visible to have a width.
function placeAt(el, rect) {
  const panel = document.body.classList.contains('gmp-panel-open') ? PANEL_WIDTH : 0;
  const maxLeft = document.documentElement.clientWidth - panel - el.offsetWidth - 16;
  el.style.top = rect.bottom + window.scrollY + 6 + 'px';
  el.style.left = Math.max(0, Math.min(rect.right, maxLeft)) + window.scrollX + 'px';
}

function placeAddButton(article, e) {
  if (!content) return;
  let block = e.target.closest?.('[data-src]');
  if (!block || !content.contains(block)) {
    block = [...content.querySelectorAll('[data-src]')].find((el) => {
      const r = el.getBoundingClientRect();
      return e.clientY >= r.top && e.clientY <= r.bottom;
    });
  }
  if (!block) return (ui.addButton.hidden = true);
  const r = block.getBoundingClientRect();
  ui.addButton.block = block;
  ui.addButton.style.top = r.top + window.scrollY + 'px';
  ui.addButton.style.left = article.getBoundingClientRect().left + window.scrollX + 14 + 'px';
  ui.addButton.hidden = false;
}

function openComposer(anchor) {
  ui.selButton.hidden = true;
  ui.closeComposer?.(false);
  const wasOpen = document.body.classList.contains('gmp-panel-open');
  const input = h('textarea', { rows: 4, placeholder: 'Leave a comment' });
  // A cancelled comment puts the panel back the way it was. A saved one leaves it open on the new thread.
  const close = (saved) => {
    ui.closeComposer = null;
    ui.composer.hidden = true;
    ui.composer.replaceChildren();
    setPending(null);
    if (!saved && !wasOpen) setPanel(false);
  };
  const save = async () => {
    const body = input.value.trim();
    if (!body) return;
    const { rect, range, el, ...fields } = anchor;
    const out = await api('thread', { path: ctx.path, body, ...fields });
    if (!out) return;
    close(true);
    getSelection().removeAllRanges();
    activeId = out.thread_id;
    paint();
    renderPanel();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) save();
    if (e.key === 'Escape') close(false);
  });
  const preview = anchor.quote
    ? h('blockquote', {}, truncate(anchor.quote, 160))
    : h('div', { class: 'gmp-lines' }, `Lines ${anchor.line_start}–${anchor.line_end}`);
  ui.composer.replaceChildren(
    h('div', { class: 'gmp-composer-title' }, 'New comment'),
    preview,
    input,
    h('div', { class: 'gmp-actions' },
      h('span', { class: 'gmp-hint' }, '⌘↵ to save'),
      h('button', { onclick: () => close(false) }, 'Cancel'),
      h('button', { class: 'gmp-primary', onclick: save }, 'Comment')),
  );
  ui.closeComposer = close;
  ui.composer.hidden = false;
  setPending(anchor);
  setPanel(true);
  ui.panel.scrollTop = 0;
  input.focus();
}

// Marks what the open composer is about, since focusing the textarea takes the page selection away.
function setPending(anchor) {
  document.querySelectorAll('.gmp-block-pending').forEach((e) => e.classList.remove('gmp-block-pending'));
  if (window.CSS?.highlights) CSS.highlights.delete('gmp-pending');
  if (!anchor) return;
  if (anchor.range && window.Highlight) CSS.highlights.set('gmp-pending', new Highlight(anchor.range));
  if (anchor.el) anchor.el.classList.add('gmp-block-pending');
}

function setPanel(open) {
  if (!open) ui.closeComposer?.(true);
  document.body.classList.toggle('gmp-panel-open', open);
  ctx.store.set('gmp.panel', open);
  ui.toolsButton.setAttribute('aria-pressed', String(open));
}

function focusThread(id, scroll) {
  activeId = id;
  paint();
  setPanel(true);
  renderPanel();
  ui.panel.querySelector(`[data-id="${id}"]`)?.scrollIntoView({ block: 'nearest' });
  const a = anchors.get(id);
  if (scroll && a) (a.range?.startContainer.parentElement || a.el).scrollIntoView({ block: 'center', behavior: 'smooth' });
}

const truncate = (s, n) => (s.length > n ? s.slice(0, n - 1) + '…' : s);

function agentStatus() {
  const { listening, working_since: working, pending } = state.agent || {};
  if (working) return ['working', 'Agent is working on your feedback'];
  if (pending) return [listening ? 'listening' : 'idle', listening ? 'Sent, agent picking it up' : 'Sent, waiting for an agent'];
  return listening ? ['listening', 'Agent listening'] : ['idle', 'No agent listening'];
}

function renderPanel() {
  if (ui.cards.contains(document.activeElement) && document.activeElement.tagName === 'TEXTAREA') {
    panelDirty = true;
    return;
  }
  panelDirty = false;
  const open = state.threads.filter((t) => t.status === 'open');
  ui.toolsButton.textContent = '\u{1F4AC} ' + open.length;
  const shown = state.threads.filter((t) => showResolved || t.status === 'open' || t.id === activeId);
  const [dot, status] = agentStatus();
  const resolvedCount = state.threads.length - open.length;
  ui.panelHead.replaceChildren(
    h('header', {},
      h('strong', {}, 'Comments'),
      h('label', {}, h('input', { type: 'checkbox', checked: showResolved, onchange: (e) => { showResolved = e.target.checked; renderPanel(); } }), ` Resolved (${resolvedCount})`),
      h('button', { class: 'gmp-close', title: 'Close (c)', onclick: () => setPanel(false) }, '×')),
    h('div', { class: 'gmp-status' }, h('span', { class: 'gmp-dot gmp-' + dot }), status),
  );
  ui.cards.replaceChildren(
    ...(shown.length ? shown.map(renderCard) : [h('p', { class: 'gmp-empty' }, 'No comments on this page. Select text and press c, or use the + beside a block.')]),
  );
}

function renderCard(t) {
  const anchored = anchors.get(t.id);
  const quote = t.quote ? h('blockquote', {}, truncate(t.quote, 240)) : h('div', { class: 'gmp-lines' }, `Lines ${t.line_start}–${t.line_end}`);
  const messages = t.messages.map((m) => h('div', { class: 'gmp-msg gmp-' + m.author },
    h('div', { class: 'gmp-meta', title: new Date(m.created_at * 1000).toLocaleString() },
      m.author === 'agent' ? 'agent' : 'you',
      m.state === 'draft' ? h('span', { class: 'gmp-badge' }, 'draft') : null,
      m.state === 'sent' ? h('span', { class: 'gmp-badge' }, 'sent') : null,
      m.state === 'draft' ? h('button', { class: 'gmp-link', onclick: () => api('delete', { message_id: m.id }) }, 'delete') : null),
    h('div', { class: 'gmp-body' }, formatBody(m.body))));
  let footer;
  if (t.status === 'open') {
    const input = h('textarea', { rows: 2, placeholder: 'Reply' });
    input.value = replyText.get(t.id) || '';
    input.addEventListener('input', () => replyText.set(t.id, input.value));
    const reply = async () => {
      const body = input.value.trim();
      if (!body) return;
      replyText.delete(t.id);
      input.blur();
      await api('message', { thread_id: t.id, body });
    };
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) reply();
    });
    footer = [input, h('div', { class: 'gmp-actions' },
      h('button', { onclick: reply }, 'Reply'),
      h('button', { onclick: () => api('status', { thread_id: t.id, status: 'resolved' }) }, 'Resolve'))];
  } else {
    footer = h('div', { class: 'gmp-actions' }, h('button', { onclick: () => api('status', { thread_id: t.id, status: 'open' }) }, 'Reopen'));
  }
  return h('div', {
    class: ['gmp-card', t.id === activeId && 'gmp-active', t.status !== 'open' && 'gmp-resolved'].filter(Boolean).join(' '),
    'data-id': t.id,
    onclick: (e) => { if (!e.target.closest('button, textarea, input')) focusThread(t.id, true); },
  }, quote, anchored ? null : h('span', { class: 'gmp-badge gmp-outdated' }, 'outdated'), messages, footer);
}

function renderWidget() {
  const total = state.drafts;
  const here = state.threads.reduce((n, t) => n + t.messages.filter((m) => m.state === 'draft').length, 0);
  const [dot, status] = agentStatus();
  const busy = state.agent?.working_since || state.agent?.pending;
  ui.widget.hidden = !total && !busy;
  if (ui.widget.hidden) return;
  const children = [h('span', { class: 'gmp-dot gmp-' + dot }), h('span', {}, status)];
  if (total) {
    const elsewhere = total - here;
    children.push(
      h('span', { class: 'gmp-sep' }),
      h('span', {}, `${total} draft${total === 1 ? '' : 's'}` + (elsewhere ? ` (${elsewhere} on other pages)` : '')),
      h('button', { class: 'gmp-primary', onclick: () => api('submit') }, 'Submit feedback'));
  }
  ui.widget.replaceChildren(...children);
}

function injectStyle() {
  document.head.append(h('style', {}, `
    ::highlight(gmp-comment) { background-color: rgba(212, 167, 44, 0.3); }
    ::highlight(gmp-active) { background-color: rgba(212, 167, 44, 0.65); }
    ::highlight(gmp-pending) { background-color: rgba(84, 174, 255, 0.4); }
    .gmp-block-pending { box-shadow: -6px 0 0 var(--accent); }
    #gmp-widget[hidden], .gmp-add[hidden], .gmp-sel[hidden], .gmp-composer[hidden] { display: none; }
    .gmp-block { box-shadow: -6px 0 0 rgba(212, 167, 44, 0.55); }
    .gmp-block-active { box-shadow: -6px 0 0 rgba(212, 167, 44, 1); background-color: rgba(212, 167, 44, 0.12); }
    body.gmp-panel-open main { margin-right: ${PANEL_WIDTH}px; }
    body.gmp-panel-open #tools { right: ${PANEL_WIDTH + 16}px; }
    body:not(.gmp-panel-open) #gmp-panel { display: none; }
    #gmp-panel {
      position: fixed; top: 0; right: 0; width: ${PANEL_WIDTH}px; height: 100vh; box-sizing: border-box; overflow: auto;
      padding: 12px 12px 72px; background: var(--side-bg); border-left: 1px solid var(--border); z-index: 2;
    }
    #gmp-panel header { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
    #gmp-panel header label { color: var(--muted); font-size: 12px; margin-left: auto; }
    .gmp-close { border: none; background: none; color: var(--muted); font-size: 18px; cursor: pointer; padding: 0 4px; }
    .gmp-status { display: flex; align-items: center; gap: 6px; color: var(--muted); font-size: 12px; margin-bottom: 12px; }
    .gmp-empty { color: var(--muted); }
    .gmp-card { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px; margin-bottom: 10px; cursor: pointer; }
    .gmp-card.gmp-active { border-color: var(--accent); }
    .gmp-card.gmp-resolved { opacity: 0.7; }
    .gmp-card blockquote, .gmp-composer blockquote {
      margin: 0 0 8px; padding: 0 8px; border-left: 3px solid rgba(212, 167, 44, 0.8); color: var(--muted);
      white-space: pre-wrap; overflow-wrap: anywhere;
    }
    .gmp-lines { color: var(--muted); font-size: 12px; margin-bottom: 8px; font-family: ui-monospace, monospace; }
    .gmp-msg { margin: 8px 0; }
    .gmp-meta { display: flex; align-items: center; gap: 6px; font-size: 12px; font-weight: 600; }
    .gmp-agent .gmp-meta { color: var(--accent); }
    .gmp-body { white-space: pre-wrap; overflow-wrap: anywhere; margin-top: 2px; }
    .gmp-body code { font-size: 85%; padding: 0.1em 0.3em; border-radius: 4px; background: var(--hover); }
    .gmp-badge { font-weight: 400; font-size: 11px; color: var(--muted); border: 1px solid var(--border); border-radius: 10px; padding: 0 6px; }
    .gmp-outdated { display: inline-block; margin-bottom: 6px; color: #bc4c00; border-color: #bc4c00; }
    .gmp-link { border: none; background: none; padding: 0; color: var(--muted); font-size: 11px; cursor: pointer; text-decoration: underline; }
    #gmp-panel textarea, .gmp-composer textarea {
      box-sizing: border-box; width: 100%; font: inherit; color: var(--fg); background: var(--bg);
      border: 1px solid var(--border); border-radius: 6px; padding: 6px 8px; resize: vertical;
    }
    .gmp-actions { display: flex; justify-content: flex-end; align-items: center; gap: 6px; margin-top: 6px; }
    .gmp-actions button, #gmp-widget button, .gmp-sel {
      padding: 3px 10px; font: inherit; font-size: 12px; cursor: pointer; color: var(--fg); background: var(--side-bg);
      border: 1px solid var(--border); border-radius: 6px;
    }
    button.gmp-primary { color: #fff; background: #1f883d; border-color: #1f883d; }
    .gmp-hint { color: var(--muted); font-size: 11px; margin-right: auto; }
    .gmp-composer { background: var(--bg); border: 1px solid var(--accent); border-radius: 6px; padding: 10px; margin-bottom: 10px; }
    .gmp-composer-title { font-weight: 600; font-size: 12px; margin-bottom: 8px; }
    .gmp-sel { position: absolute; z-index: 3; box-shadow: 0 2px 8px rgba(0, 0, 0, 0.15); }
    .gmp-add {
      position: absolute; z-index: 1; width: 22px; height: 22px; padding: 0; line-height: 20px; font-size: 15px;
      cursor: pointer; color: #fff; background: var(--accent); border: none; border-radius: 6px;
    }
    #gmp-widget {
      position: fixed; bottom: 16px; right: 16px; z-index: 3; display: flex; align-items: center; gap: 8px;
      padding: 8px 12px; background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.2);
    }
    body.gmp-panel-open #gmp-widget { right: ${PANEL_WIDTH + 16}px; }
    .gmp-sep { width: 1px; align-self: stretch; background: var(--border); }
    .gmp-dot { width: 8px; height: 8px; border-radius: 50%; flex: none; background: var(--muted); }
    .gmp-dot.gmp-listening { background: #1f883d; }
    .gmp-dot.gmp-working { background: #d4a72c; animation: gmp-pulse 1s ease-in-out infinite alternate; }
    @keyframes gmp-pulse { to { opacity: 0.3; } }
    .gmp-error { color: #cf222e; }
  `));
}
"""


class Handler(SimpleHTTPRequestHandler):
    root: Path
    feedback = False
    # Every feedback request must carry this. It keeps other pages open in the browser from posting comments, which
    # an agent would then act on.
    token = ""

    def log_message(self, format, *args):  # noqa: A002 (matches the base signature)
        pass

    def do_POST(self):
        url = urlparse(self.path)
        try:
            body = self.read_json()
        except ValueError as err:
            return self.send_error(HTTPStatus.BAD_REQUEST, str(err))
        if url.path == "/__prefs":
            save_prefs(self.root, body)
            self.send_response(HTTPStatus.NO_CONTENT)
            return self.end_headers()
        action = FEEDBACK_ACTIONS.get(url.path.removeprefix("/__feedback/"))
        if not url.path.startswith("/__feedback/") or action is None or not self.feedback_allowed():
            return self.send_error(HTTPStatus.NOT_FOUND)
        try:
            with feedback_db(self.root) as conn:
                out = action(self, conn, body)
        except (KeyError, TypeError, ValueError) as err:
            return self.send_error(HTTPStatus.BAD_REQUEST, f"bad request: {err}")
        self.send_bytes(json.dumps(out).encode(), "application/json")

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/__mtime":
            return self.send_mtime(parse_qs(url.query).get("path", [""])[0])
        if url.path == "/__tree":
            return self.send_bytes(json.dumps(build_tree(self.root)).encode(), "application/json")
        if url.path == "/__feedback.js" and self.feedback:
            return self.send_bytes(FEEDBACK_JS.encode(), "text/javascript; charset=utf-8")
        if url.path == "/__feedback/state":
            if not self.feedback_allowed():
                return self.send_error(HTTPStatus.NOT_FOUND)
            with feedback_db(self.root) as conn:
                state = feedback_state(conn, self.rel_file(parse_qs(url.query).get("path", [""])[0]))
            return self.send_bytes(json.dumps(state).encode(), "application/json")

        target = self.resolve(url.path)
        if target is None:
            return self.send_error(HTTPStatus.NOT_FOUND)
        if target.is_dir():
            if not url.path.endswith("/"):
                return self.redirect(self.url_for(target))
            landing = index_file(target)
            if landing:
                return self.redirect(self.url_for(landing))
            return self.send_page(target, "dir")
        if target.suffix.lower() in MARKDOWN_SUFFIXES and "raw" not in parse_qs(url.query):
            return self.send_page(target, "file")
        return super().do_GET()

    def resolve(self, url_path):
        target = (self.root / unquote(url_path).lstrip("/")).resolve()
        if not target.is_relative_to(self.root) or not target.exists():
            return None
        return target

    def url_for(self, target):
        return url_for(self.root, target)

    def rel_file(self, url_path):
        """Returns the root-relative path the page at url_path stores comments under."""
        target = self.resolve(url_path)
        if target is None or not target.is_file():
            raise ValueError(f"no such file: {url_path}")
        return target.relative_to(self.root).as_posix()

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("expected an object")
        return body

    def feedback_allowed(self):
        # The Host check stops DNS rebinding. The token stops cross-origin form posts, which can't set headers.
        port = self.server.server_address[1]
        host_ok = self.headers.get("Host") in (f"127.0.0.1:{port}", f"localhost:{port}")
        token_ok = secrets.compare_digest(self.headers.get("X-GMP-Token", ""), self.token)
        return self.feedback and host_ok and token_ok

    def feedback_create_thread(self, conn, body):
        file = self.rel_file(body["path"])
        text = clean_body(body["body"])
        fields = {k: str(body.get(k, "")) for k in ("quote", "prefix", "suffix", "context")}
        lines = (int(body["line_start"]), int(body["line_end"]))
        now = time.time()
        with transaction(conn):
            thread_id = conn.execute(
                "INSERT INTO threads (file, quote, prefix, suffix, line_start, line_end, context, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (file, fields["quote"], fields["prefix"], fields["suffix"], *lines, fields["context"], now),
            ).lastrowid
            add_message(conn, thread_id, "user", text)
        return {"thread_id": thread_id}

    def feedback_add_message(self, conn, body):
        thread = get_thread(conn, int(body["thread_id"]))
        if thread["status"] != "open":
            raise ValueError("thread is resolved")
        return {"message_id": add_message(conn, thread["id"], "user", clean_body(body["body"]))}

    def feedback_delete_draft(self, conn, body):
        with transaction(conn):
            row = conn.execute(
                "SELECT thread_id FROM messages WHERE id = ? AND author = 'user' AND batch_id IS NULL",
                (int(body["message_id"]),),
            ).fetchone()
            if row is None:
                raise ValueError("not a draft")
            conn.execute("DELETE FROM messages WHERE id = ?", (int(body["message_id"]),))
            conn.execute(
                "DELETE FROM threads WHERE id = ? AND NOT EXISTS (SELECT 1 FROM messages WHERE thread_id = ?)",
                (row["thread_id"], row["thread_id"]),
            )
        return {}

    def feedback_set_status(self, conn, body):
        status = body["status"]
        if status not in ("open", "resolved"):
            raise ValueError(f"unknown status {status!r}")
        thread = get_thread(conn, int(body["thread_id"]))
        conn.execute("UPDATE threads SET status = ? WHERE id = ?", (status, thread["id"]))
        return {}

    def feedback_submit(self, conn, body):
        with transaction(conn):
            if not conn.execute("SELECT 1 FROM messages WHERE author = 'user' AND batch_id IS NULL").fetchone():
                raise ValueError("no drafts to submit")
            batch_id = conn.execute("INSERT INTO batches (submitted_at) VALUES (?)", (time.time(),)).lastrowid
            conn.execute("UPDATE messages SET batch_id = ? WHERE author = 'user' AND batch_id IS NULL", (batch_id,))
        return {"batch_id": batch_id}

    def translate_path(self, path):
        target = self.resolve(urlparse(path).path)
        return str(target) if target else str(self.root / "__missing__")

    def send_mtime(self, url_path):
        target = self.resolve(url_path)
        if target is None:
            mtime = None
        elif target.is_dir():
            # A directory listing changes when markdown files are added or removed anywhere below it.
            mtime = hash(json.dumps(build_tree(self.root)))
        else:
            mtime = target.stat().st_mtime_ns
        self.send_bytes(json.dumps({"mtime": mtime}).encode(), "application/json")

    def send_page(self, target, kind):
        page = (
            PAGE.replace("__TITLE__", html.escape(target.name or self.root.name))
            .replace("__ROOT__", html.escape(self.root.name))
            .replace("__CRUMBS__", self.crumbs(target))
            .replace("__PATH__", json.dumps(self.url_for(target)))
            .replace("__KIND__", json.dumps(kind))
            .replace("__FEEDBACK__", json.dumps({"token": self.token}) if self.feedback else "null")
            .replace("__PREFS__", json.dumps(load_prefs(self.root)).replace("</", "<\\/"))
        )
        self.send_bytes(page.encode(), "text/html; charset=utf-8")

    def crumbs(self, target):
        parts = [f'<a href="/">{html.escape(self.root.name)}</a>']
        current = self.root
        for name in target.relative_to(self.root).parts:
            current = current / name
            parts.append(f'<a href="{self.url_for(current)}">{html.escape(name)}</a>')
        return " / ".join(parts)

    def redirect(self, location):
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.end_headers()

    def send_bytes(self, body, content_type):
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


FEEDBACK_ACTIONS = {
    "thread": Handler.feedback_create_thread,
    "message": Handler.feedback_add_message,
    "delete": Handler.feedback_delete_draft,
    "status": Handler.feedback_set_status,
    "submit": Handler.feedback_submit,
}


def url_for(root, target):
    rel = target.relative_to(root).as_posix()
    if target.is_dir():
        return "/" if rel == "." else "/" + quote(rel) + "/"
    return "/" + quote(rel)


def markdown_files(root):
    """Returns markdown paths relative to root. Uses git when root is a repo so ignored files stay out."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard", "--full-name",
             "--", *(f"*{s}" for s in MARKDOWN_SUFFIXES)],
            capture_output=True, text=True, check=True,
        )
        prefix = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--show-prefix"], capture_output=True, text=True, check=True
        ).stdout.strip()
        files = [f[len(prefix):] for f in out.stdout.splitlines() if f.startswith(prefix)]
        return sorted(f for f in files if (root / f).is_file())[:MAX_TREE_FILES]
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass

    files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SKIP_DIRS)
        for name in filenames:
            if name.lower().endswith(MARKDOWN_SUFFIXES):
                files.append(Path(dirpath, name).relative_to(root).as_posix())
                if len(files) >= MAX_TREE_FILES:
                    return sorted(files)
    return sorted(files)


def load_prefs(root):
    """Returns global preferences overlaid with the ones saved for this root."""
    data = read_prefs_file()
    return {**data.get("global", {}), **data.get("roots", {}).get(str(root), {})}


def save_prefs(root, updates):
    with PREFS_LOCK:
        data = read_prefs_file()
        for key, value in updates.items():
            if not key.startswith("gmp."):
                continue
            if key in ROOT_PREFS:
                bucket = data.setdefault("roots", {}).setdefault(str(root), {})
            else:
                bucket = data.setdefault("global", {})
            bucket[key] = value
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PREFS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
        tmp.replace(PREFS_PATH)


def read_prefs_file():
    try:
        data = json.loads(PREFS_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def feedback_db_path(root):
    digest = hashlib.sha1(str(root).encode()).hexdigest()[:12]
    return FEEDBACK_DIR / f"{root.name}-{digest}.db"


@contextlib.contextmanager
def feedback_db(root, create=True):
    path = feedback_db_path(root)
    if not create and not path.exists():
        sys.exit(f"no feedback yet for {root} (looked for {path})")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(FEEDBACK_SCHEMA)
        conn.execute("INSERT OR IGNORE INTO meta (key, value) VALUES ('root', ?)", (str(root),))
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def transaction(conn):
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def clean_body(text):
    text = str(text).strip()
    if not text:
        raise ValueError("empty message")
    return text


def get_thread(conn, thread_id):
    row = conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
    if row is None:
        raise ValueError(f"no thread {thread_id}")
    return row


def add_message(conn, thread_id, author, body):
    return conn.execute(
        "INSERT INTO messages (thread_id, author, body, created_at) VALUES (?, ?, ?, ?)",
        (thread_id, author, body, time.time()),
    ).lastrowid


def get_meta(conn, key):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn, key, value):
    conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                 (key, str(value)))


def load_messages(conn, where, params):
    """Returns {thread_id: [message]} for the messages of threads matching where, oldest first."""
    rows = conn.execute(
        "SELECT m.*, b.claimed_at FROM messages m JOIN threads t ON t.id = m.thread_id"
        f" LEFT JOIN batches b ON b.id = m.batch_id WHERE {where} ORDER BY m.id",
        params,
    )
    out = {}
    for m in rows:
        if m["author"] == "agent":
            state = "agent"
        elif m["batch_id"] is None:
            state = "draft"
        else:
            state = "sent" if m["claimed_at"] is None else "seen"
        out.setdefault(m["thread_id"], []).append(
            {"id": m["id"], "author": m["author"], "body": m["body"], "created_at": m["created_at"],
             "batch_id": m["batch_id"], "state": state}
        )
    return out


def feedback_state(conn, file):
    messages = load_messages(conn, "t.file = ?", (file,))
    threads = [
        {**dict(t), "messages": messages.get(t["id"], [])}
        for t in conn.execute("SELECT * FROM threads WHERE file = ? ORDER BY id", (file,))
    ]
    drafts = conn.execute("SELECT COUNT(*) FROM messages WHERE author = 'user' AND batch_id IS NULL").fetchone()[0]
    return {"threads": threads, "drafts": drafts, "agent": agent_status(conn)}


def agent_status(conn):
    heartbeat = float(get_meta(conn, "heartbeat") or 0)
    working = conn.execute(
        "SELECT MIN(claimed_at) FROM batches WHERE claimed_at IS NOT NULL AND done_at IS NULL"
    ).fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM batches WHERE claimed_at IS NULL").fetchone()[0]
    return {"listening": time.time() - heartbeat < HEARTBEAT_FRESH, "working_since": working, "pending": pending}


def index_file(directory):
    """Returns the folder's README or index markdown file, preferring README, or None."""
    candidates = {}
    for child in directory.iterdir():
        stem, _, suffix = child.name.lower().rpartition(".")
        if child.is_file() and "." + suffix in MARKDOWN_SUFFIXES and stem in INDEX_NAMES:
            candidates.setdefault(INDEX_NAMES.index(stem), child)
    return candidates[min(candidates)] if candidates else None


def sort_key(node):
    name = node["name"].lower()
    if "children" in node:
        return (1, name)
    if name.rsplit(".", 1)[0] in INDEX_NAMES:
        return (0, INDEX_NAMES.index(name.rsplit(".", 1)[0]), name)
    return (2, name)


def build_tree(root):
    """Builds a nested {name, path, children} tree of the markdown files under root.

    Within a folder, a README or index file comes first, then subfolders, then the remaining files.
    """
    top = {"name": root.name, "path": "/", "children": {}}
    for rel in markdown_files(root):
        node = top
        parts = rel.split("/")
        for i, name in enumerate(parts[:-1]):
            node = node["children"].setdefault(
                name, {"name": name, "path": "/" + quote("/".join(parts[: i + 1])) + "/", "children": {}}
            )
        node["children"][parts[-1]] = {"name": parts[-1], "path": "/" + quote(rel)}

    def finish(node):
        if "children" not in node:
            return node
        kids = sorted(node["children"].values(), key=sort_key)
        return {**node, "children": [finish(c) for c in kids]}

    return finish(top)


def git_toplevel(path):
    try:
        out = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True
        )
        return Path(out.stdout.strip()).resolve()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def feedback_main(argv):
    parser = argparse.ArgumentParser(
        prog="preview.py feedback", description="Read and answer comments left in a preview started with --feedback."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, text):
        p = sub.add_parser(name, help=text, description=text)
        p.add_argument("--root", help="the served root (default: the git toplevel of the current directory)")
        return p

    wait = add("wait", "Block until a batch of feedback is submitted, then claim it and print it.")
    wait.add_argument("--timeout", type=float, default=0, help="seconds to wait before giving up (default: forever)")
    show = add("show", "Print open threads and their submitted messages.")
    show.add_argument("--all", action="store_true", help="include resolved threads")
    reply = add("reply", "Post an agent reply to a thread.")
    reply.add_argument("thread", type=int)
    reply.add_argument("body", nargs="?", help='the reply, or "-" to read it from stdin')
    reply.add_argument("--body-file", help="read the reply from this file")
    sub.add_parser("roots", help="List served roots that have feedback.", description="List served roots that have feedback.")
    args = parser.parse_args(argv)

    if args.command == "roots":
        return feedback_roots()
    root = Path(args.root).resolve() if args.root else (git_toplevel(Path.cwd()) or Path.cwd().resolve())
    try:
        if args.command == "wait":
            feedback_wait(root, args.timeout)
        elif args.command == "show":
            feedback_show(root, args.all)
        elif args.command == "reply":
            if args.body_file:
                text = Path(args.body_file).read_text()
            elif args.body == "-":
                text = sys.stdin.read()
            elif args.body is not None:
                text = args.body
            else:
                parser.error("reply needs a body, - for stdin, or --body-file")
            with feedback_db(root, create=False) as conn:
                get_thread(conn, args.thread)
                add_message(conn, args.thread, "agent", clean_body(text))
            print(f"Replied to thread {args.thread}.")
    except ValueError as err:
        sys.exit(str(err))
    except KeyboardInterrupt:
        sys.exit(130)


def feedback_wait(root, timeout):
    """Claims the oldest unclaimed batch, waiting for one if needed, and prints it.

    Starting a wait marks earlier claimed batches done, since the agent only waits again once it has handled them.
    """

    def stop(*_):
        sys.exit(143)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)
    deadline = time.time() + timeout if timeout else None
    with feedback_db(root) as conn:
        conn.execute("UPDATE batches SET done_at = ? WHERE claimed_at IS NOT NULL AND done_at IS NULL", (time.time(),))
        beat = 0.0
        try:
            while True:
                now = time.time()
                if now - beat >= HEARTBEAT_EVERY:
                    set_meta(conn, "heartbeat", now)
                    beat = now
                batch_id = claim_batch(conn)
                if batch_id is not None:
                    print(format_batch(conn, root, batch_id))
                    return
                if deadline and now >= deadline:
                    print(f"No feedback in {timeout:g}s. Wait again to keep listening:\n  {agent_command(root, 'wait')}")
                    return
                time.sleep(0.5)
        finally:
            set_meta(conn, "heartbeat", 0)


def claim_batch(conn):
    if not conn.execute("SELECT 1 FROM batches WHERE claimed_at IS NULL").fetchone():
        return None
    with transaction(conn):
        row = conn.execute("SELECT id FROM batches WHERE claimed_at IS NULL ORDER BY id LIMIT 1").fetchone()
        if row:
            conn.execute("UPDATE batches SET claimed_at = ? WHERE id = ?", (time.time(), row["id"]))
    return row["id"] if row else None


def format_batch(conn, root, batch_id):
    ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT thread_id FROM messages WHERE batch_id = ? ORDER BY thread_id", (batch_id,)
    )]
    threads = [get_thread(conn, i) for i in ids]
    messages = load_messages(conn, f"t.id IN ({', '.join('?' * len(ids))})", ids)
    out = [f"# Feedback batch {batch_id}: {len(ids)} thread{'' if len(ids) == 1 else 's'} in {root}", ""]
    out += format_threads(root, threads, messages, batch_id)
    out += [
        "Messages marked (new) arrived in this batch. Make the changes, then answer each thread:",
        f"  {agent_command(root, 'reply')} THREAD \"reply\"",
        "  (use --body-file FILE instead of the quoted reply for anything long or multi-line)",
        "The user resolves threads. When you're done, listen for the next batch:",
        f"  {agent_command(root, 'wait')}",
    ]
    return "\n".join(out)


def feedback_show(root, include_resolved):
    with feedback_db(root, create=False) as conn:
        where = "1 = 1" if include_resolved else "t.status = 'open'"
        threads = conn.execute(f"SELECT * FROM threads t WHERE {where} ORDER BY file, line_start, id").fetchall()
        messages = load_messages(conn, where, ())
    if not threads:
        print("No open threads." if not include_resolved else "No threads.")
        return
    print("\n".join(format_threads(root, threads, messages, None)).rstrip())


def format_threads(root, threads, messages, batch_id):
    out = []
    for t in threads:
        out.append(f"## Thread {t['id']} ({t['status']})")
        out.append(f"File: {root / t['file']}")
        out.append(f"Lines when commented: {t['line_start']}-{t['line_end']}")
        if t["quote"]:
            out.append("Quote: " + json.dumps(t["quote"], ensure_ascii=False))
        else:
            out.append("Quote: none; the comment is on the whole block")
        out.append("Source when commented:")
        out += ["    " + line for line in t["context"].splitlines() or [""]]
        out.append("Messages:")
        for m in messages.get(t["id"], []):
            if m["state"] == "draft":
                continue
            tag = " (new)" if batch_id is not None and m["batch_id"] == batch_id else ""
            first, *rest = m["body"].splitlines() or [""]
            out.append(f"- {m['author']}{tag}: {first}")
            out += ["  " + line for line in rest]
        out.append("")
    return out


def feedback_roots():
    found = False
    for path in sorted(FEEDBACK_DIR.glob("*.db")):
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            root = get_meta(conn, "root")
            count = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
            open_threads = count("SELECT COUNT(*) FROM threads WHERE status = 'open'")
            drafts = count("SELECT COUNT(*) FROM messages WHERE author = 'user' AND batch_id IS NULL")
            pending = count("SELECT COUNT(*) FROM batches WHERE claimed_at IS NULL")
            listening = agent_status(conn)["listening"]
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        found = True
        print(f"{root}\n  open threads: {open_threads}, drafts: {drafts}, unclaimed batches: {pending}, "
              f"agent {'listening' if listening else 'not listening'}")
    if not found:
        print("No feedback yet.")


def agent_command(root, command):
    return f"{SCRIPT} feedback {command} --root {shlex.quote(str(root))}"


def main():
    if sys.argv[1:2] == ["feedback"]:
        return feedback_main(sys.argv[2:])
    parser = argparse.ArgumentParser(
        description="Preview markdown with GitHub styling and Mermaid.",
        epilog="Run `preview.py feedback --help` for the commands an agent uses to read and answer comments.",
    )
    parser.add_argument("path", nargs="?", default=".", help="markdown file or directory to open (default: .)")
    parser.add_argument("--root", help="directory to serve (default: the git toplevel of PATH, else PATH)")
    parser.add_argument("--port", type=int, default=0, help="port to listen on (default: one the OS picks)")
    parser.add_argument("--no-open", action="store_true", help="don't open a browser")
    parser.add_argument("--feedback", action="store_true", help="let the page collect comments for an agent")
    args = parser.parse_args()

    target = Path(args.path).resolve()
    if not target.exists():
        sys.exit(f"no such file or directory: {args.path}")
    start_dir = target if target.is_dir() else target.parent
    root = Path(args.root).resolve() if args.root else (git_toplevel(start_dir) or start_dir)
    if not target.is_relative_to(root):
        sys.exit(f"{target} is outside the served root {root}")

    Handler.root = root
    Handler.feedback = args.feedback
    Handler.token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}{url_for(root, target)}"
    print(f"serving {root}\nopen {url}", flush=True)
    if args.feedback:
        print(f"feedback on, stored in {feedback_db_path(root)}\nagent listens with: {agent_command(root, 'wait')}")
    print("ctrl-c to stop", flush=True)
    if not args.no_open:
        threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
