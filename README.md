# gh-markdown-preview

Local preview of markdown with GitHub styling, Mermaid diagrams, syntax highlighting, and live reload. It's a single Python file with no dependencies beyond the standard library. Rendering happens in the browser with `marked`, `github-markdown-css`, `highlight.js`, and `mermaid`, all pinned and loaded from jsDelivr, so the browser needs network access.

```sh
./preview.py path/to/doc.md        # open one file
./preview.py path/to/folder        # open the folder's README.md, or a listing
./preview.py . --port 6500 --no-open
```

The served root defaults to the git toplevel of the path, so relative links that climb out of the folder (`../task_processing.md`) still resolve. Pass `--root` to override it. The server binds to 127.0.0.1 and refuses paths outside the root.

The page polls the server every second and re-renders in place when the file changes, keeping the scroll position.

Differences from GitHub: no GitHub alerts (`> [!NOTE]`), no emoji shortcodes, and no relative image rewriting beyond what the browser does on its own.
