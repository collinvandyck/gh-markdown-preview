#!/usr/bin/env python3
"""Preview a tree of markdown files in the browser with GitHub styling and Mermaid.

Serves files under a root directory on localhost. A request for a .md file returns a page that fetches the raw
markdown and renders it client-side with marked, github-markdown-css, highlight.js, and mermaid. The page polls the
server and reloads when the file changes.
"""

import argparse
import html
import json
import subprocess
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

PAGE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5.5.1/github-markdown.min.css">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/styles/github.min.css"
      media="(prefers-color-scheme: light)">
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/styles/github-dark.min.css"
      media="(prefers-color-scheme: dark)">
<style>
  body { margin: 0; background: #fff; }
  @media (prefers-color-scheme: dark) { body { background: #0d1117; } }
  .markdown-body { box-sizing: border-box; max-width: 1012px; margin: 0 auto; padding: 32px 45px; }
  @media (max-width: 767px) { .markdown-body { padding: 16px; } }
  .crumbs { font-size: 12px; opacity: .7; margin-bottom: 16px; font-family: ui-monospace, monospace; }
  .mermaid { text-align: center; margin-bottom: 16px; }
  .mermaid-error { color: #cf222e; white-space: pre-wrap; }
</style>
</head>
<body>
<article class="markdown-body">
  <div class="crumbs">__CRUMBS__</div>
  <div id="content">Loading…</div>
</article>
<script type="module">
import { marked } from 'https://cdn.jsdelivr.net/npm/marked@12.0.2/lib/marked.esm.js';
import { gfmHeadingId } from 'https://cdn.jsdelivr.net/npm/marked-gfm-heading-id@3.1.3/+esm';
import hljs from 'https://cdn.jsdelivr.net/npm/@highlightjs/cdn-assets@11.9.0/es/highlight.min.js';
import mermaid from 'https://cdn.jsdelivr.net/npm/mermaid@11.4.1/dist/mermaid.esm.min.mjs';

const path = __PATH__;
const dark = window.matchMedia('(prefers-color-scheme: dark)').matches;
mermaid.initialize({ startOnLoad: false, theme: dark ? 'dark' : 'default' });
marked.use(gfmHeadingId());

async function render() {
  const res = await fetch(path + '?raw=1', { cache: 'no-store' });
  const md = await res.text();
  const content = document.getElementById('content');
  content.innerHTML = marked.parse(md);

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

  if (location.hash) {
    document.getElementById(decodeURIComponent(location.hash.slice(1)))?.scrollIntoView();
  }
}

let mtime = null;
async function poll() {
  try {
    const res = await fetch('/__mtime?path=' + encodeURIComponent(path), { cache: 'no-store' });
    const { mtime: next } = await res.json();
    if (mtime !== null && next !== mtime) {
      const y = window.scrollY;
      await render();
      window.scrollTo(0, y);
    }
    mtime = next;
  } catch (_) {
    // Server stopped. Keep the last render.
  }
  setTimeout(poll, 1000);
}

await render();
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

        target = self.resolve(url.path)
        if target is None:
            return self.send_error(HTTPStatus.NOT_FOUND)
        if target.is_dir():
            readme = target / "README.md"
            if readme.is_file():
                return self.redirect(self.url_for(readme))
            return self.send_listing(target)
        if target.suffix.lower() in (".md", ".markdown") and "raw" not in parse_qs(url.query):
            return self.send_page(target)
        return super().do_GET()

    def resolve(self, url_path):
        target = (self.root / unquote(url_path).lstrip("/")).resolve()
        if not target.is_relative_to(self.root) or not target.exists():
            return None
        return target

    def url_for(self, target):
        rel = target.relative_to(self.root).as_posix()
        return "/" + quote(rel) + ("/" if target.is_dir() and rel else "")

    def translate_path(self, path):
        target = self.resolve(urlparse(path).path)
        return str(target) if target else str(self.root / "__missing__")

    def send_mtime(self, url_path):
        target = self.resolve(url_path)
        body = json.dumps({"mtime": target.stat().st_mtime_ns if target else None}).encode()
        self.send_bytes(body, "application/json")

    def send_page(self, target):
        page = (
            PAGE.replace("__TITLE__", html.escape(target.name))
            .replace("__CRUMBS__", self.crumbs(target))
            .replace("__PATH__", json.dumps(self.url_for(target)))
        )
        self.send_bytes(page.encode(), "text/html; charset=utf-8")

    def send_listing(self, target):
        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        items = [
            f'<li><a href="{self.url_for(p)}">{html.escape(p.name)}{"/" if p.is_dir() else ""}</a></li>'
            for p in entries
            if not p.name.startswith(".") and (p.is_dir() or p.suffix.lower() in (".md", ".markdown"))
        ]
        body = (
            '<!doctype html><meta charset="utf-8">'
            '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/github-markdown-css@5.5.1/github-markdown.min.css">'
            '<article class="markdown-body" style="max-width:1012px;margin:0 auto;padding:32px 45px">'
            f'<div style="font-size:12px;opacity:.7">{self.crumbs(target)}</div><ul>{"".join(items)}</ul></article>'
        )
        self.send_bytes(body.encode(), "text/html; charset=utf-8")

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
    rel = target.relative_to(root).as_posix()
    url = f"http://127.0.0.1:{args.port}/" + quote("" if rel == "." else rel)
    print(f"serving {root}\nopen {url}\nctrl-c to stop")
    if not args.no_open:
        threading.Timer(0.3, webbrowser.open, [url]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
