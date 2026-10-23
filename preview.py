#!/usr/bin/env python3
"""Preview a tree of markdown files in the browser with GitHub styling and Mermaid.

Serves files under a root directory on localhost. A request for a .md file returns a page that fetches the raw
markdown and renders it client-side with marked, github-markdown-css, highlight.js, and mermaid. A sidebar shows the
markdown files under the root as a tree. The page polls the server and re-renders when the file changes.
"""

import argparse
import html
import json
import os
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

MARKDOWN_SUFFIXES = (".md", ".markdown")
SKIP_DIRS = {"node_modules", "vendor", "__pycache__", "dist", "build", "target"}
MAX_TREE_FILES = 20000
INDEX_NAMES = ["readme", "index"]

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
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
  let gmpTheme = 'auto';
  try { gmpTheme = JSON.parse(localStorage.getItem('gmp.theme')) || 'auto'; } catch (_) {}
  window.gmpApplyTheme(['auto', 'light', 'dark'].includes(gmpTheme) ? gmpTheme : 'auto');
  try { if (JSON.parse(localStorage.getItem('gmp.wide'))) document.documentElement.classList.add('wide'); } catch (_) {}
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
marked.use(gfmHeadingId());

const store = {
  get(key, fallback) {
    try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (_) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch (_) {}
  },
};

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
  if (e.target.tagName === 'INPUT' || e.metaKey || e.ctrlKey || e.altKey) return;
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


class Handler(SimpleHTTPRequestHandler):
    root: Path

    def log_message(self, format, *args):  # noqa: A002 (matches the base signature)
        pass

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/__mtime":
            return self.send_mtime(parse_qs(url.query).get("path", [""])[0])
        if url.path == "/__tree":
            return self.send_bytes(json.dumps(build_tree(self.root)).encode(), "application/json")

        target = self.resolve(url.path)
        if target is None:
            return self.send_error(HTTPStatus.NOT_FOUND)
        if target.is_dir():
            if not url.path.endswith("/"):
                return self.redirect(self.url_for(target))
            readme = target / "README.md"
            if readme.is_file():
                return self.redirect(self.url_for(readme))
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
        return Path(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def main():
    parser = argparse.ArgumentParser(description="Preview markdown with GitHub styling and Mermaid.")
    parser.add_argument("path", nargs="?", default=".", help="markdown file or directory to open (default: .)")
    parser.add_argument("--root", help="directory to serve (default: the git toplevel of PATH, else PATH)")
    parser.add_argument("--port", type=int, default=6419)
    parser.add_argument("--no-open", action="store_true", help="don't open a browser")
    args = parser.parse_args()

    target = Path(args.path).resolve()
    if not target.exists():
        sys.exit(f"no such file or directory: {args.path}")
    start_dir = target if target.is_dir() else target.parent
    root = Path(args.root).resolve() if args.root else (git_toplevel(start_dir) or start_dir)
    if not target.is_relative_to(root):
        sys.exit(f"{target} is outside the served root {root}")

    Handler.root = root
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}{url_for(root, target)}"
    print(f"serving {root}\nopen {url}\nctrl-c to stop", flush=True)
    if not args.no_open:
        threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
