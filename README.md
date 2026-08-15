# Jira Git Universal Pull Tool

> 📘 中文文档：[README.zh-CN.md](README.zh-CN.md)

A general-purpose desktop client for the Jira Git Integration plugin (Xiplink / BigBrassBand). It ships in two desktop flavors:

- **PyQt6 desktop app** (`main.py`): pure Python + PyQt6, no browser required; all network requests run on background threads so the UI never freezes.
- **Electron / Web desktop app** (`electron/` + `web/`): Electron loads the same Web frontend for cross-platform packaging; you can also just open it in a browser against the local backend. UI features are detailed below under "Electron / Web Desktop App".

> Both frontends share the same Python backend (`api/server.py`, default port 8787) and are feature-equivalent.

## Features

- **Dual frontend / dual mode**: choose the native PyQt6 desktop app or the Electron + Web app; switch freely between PAT (full `git clone`) and Cookie (web fetch / recursive download) authentication.
- **Unified design system**: the Web frontend uses a CSS-variable-driven light / dark dual theme, with a branded header, live status dot, and GitHub-style diff tables — modern and consistent.
- **High-performance engine**: incremental scanning (~2.7×), set-based diffing, parallel merging (~8×), and O(1) file-tree indexing; a global token-bucket rate limiter with a UI-adjustable rate.
- **Smart diff**: automatically detects CRLF / LF line-ending and whitespace-only differences (classified as "line-ending diff" rather than "modified"); structured files such as JSON / JSONC / XML are auto-formatted and expanded in the diff view, so even single-line minified files become readable line by line.
- **Resume-able downloads with bounded concurrency**: Cookie mode supports recursive whole-repo downloads (including nested files and binaries) with resume, cancellation, and a default of 4 concurrent threads.

## Two Modes

| Mode | Auth | Capabilities | Limits |
| --- | --- | --- | --- |
| **PAT mode** | Personal Access Token | `git clone` full pull (incl. nested files), local browse / preview | Requires a valid PAT and repo name under that account |
| **Cookie mode** | `JSESSIONID` session | Browse file tree (lazy), preview text files, batch / recursive download of whole repo (incl. nested files & binaries), resume, parallel download | Binaries can only be "downloaded" locally, not previewed; depends on a valid session cookie |

> Cookie mode already supports **recursive whole-repo downloads** (the plugin API itself accepts any path, including subdirs and nested files),
> no longer limited to "root only". Resume + bounded concurrency (default 4 threads) make whole-repo fetching resumable, cancelable, and faster.

## Project Structure

```
jira-git-gui/
├── main.py                 # Entry: creates QApplication + MainWindow (PyQt6 desktop app)
├── run_merge.py            # CLI: merge remote repos' latest code into local (cache-first + sync history)
├── server.py               # ⚠️ Legacy monolithic backend (DEPRECATED, do not use; main path is api/server.py)
├── requirements.txt        # PyQt6 / httpx / fastapi / uvicorn
├── core/                   # Core logic layer (no GUI dependency, independently testable)
│   ├── app_paths.py        # Runtime writable dirs (relocates to ~/.jira-git-gui when frozen)
│   ├── constants.py        # Directories / proxy / timeouts
│   ├── models.py           # ConnectConfig / RepoInfo / TreeEntry / DiffResult
│   ├── config.py           # Auto-load default connection config from .env
│   ├── client.py           # JiraGitClient: connect / discover / list_level / get_file / clone / download
│   ├── cache.py            # Remote tree / content JSON cache (lock-guarded, avoids re-fetch)
│   ├── differ.py           # Diffing: compute_diff / scan_local / merge_to_local / file_diff / canonical_text
│   ├── throttle.py         # Global token-bucket rate limiter (DEFAULT_REQUEST_QPS)
│   ├── sync_history.py     # Sync history (git-log-like)
│   ├── logger.py           # Rotating file log + LogBridge (UI bridge) + global excepthook (PyQt6 lazy-loaded)
│   ├── safe.py             # safe_slot decorator: catches slot exceptions, prevents UI crashes
│   └── errors.py           # Unified exception types
├── gui/                    # UI layer (PyQt6 widgets)
│   ├── main_window.py      # Layout + signal binding + async task orchestration
│   ├── connect_dialog.py   # Connection settings (url / account / mode / PAT / Cookie / repo)
│   ├── repo_panel.py       # Discover repos / specify repo manually
│   ├── tree_panel.py       # Lazy file tree (O(1) index)
│   ├── preview_panel.py    # Code preview
│   ├── diff_panel.py       # Diff view (zero-dependency syntax highlight)
│   ├── highlighter.py      # Zero-dependency syntax highlighter (QSyntaxHighlighter)
│   ├── styles.py           # Light / dark dual-theme QSS
│   ├── commit_panel.py     # Commit history
│   └── log_panel.py        # Logs
├── workers/                # Async task layer
│   └── tasks.py            # Generic QThread Worker (auto on_log callback; full traceback on error)
├── api/                    # Backend shared by Web / Electron
│   └── server.py           # FastAPI: REST + SSE, default port 8787 (main path)
├── electron/               # Electron desktop app
│   ├── main.js             # Main process: Python backend lifecycle + BrowserWindow + log bridge
│   ├── preload.js          # Exposes window.electronAPI (contextIsolation isolated)
│   └── package.json        # name / version / start|dev|dist scripts + electron-builder config
├── web/                    # Web frontend (shared by Electron / browser, zero framework deps)
│   ├── index.html          # Page structure (toolbar + tabs + connection dialog)
│   ├── styles.css          # Design system (CSS variables, light / dark dual theme)
│   └── app.js              # Frontend logic (REST + SSE, pure vanilla JS)
├── build/                  # PyInstaller specs (gui / backend)
├── tests/                  # Unit tests (unit before integration, version-controlled)
├── store/                  # Runtime artifacts (git clone / downloads, gitignored)
├── logs/                   # Runtime logs (full traceback, gitignored)
└── docs/
    └── PACKAGING.md        # Packaging & cross-platform release details
```

Dependency direction: `gui → workers → core`; `core` does not depend back on GUI, so it can be reused and tested in isolation.

## Running

### PyQt6 desktop app

```bash
# 1. Create and activate a venv (skip if venv already exists)
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Launch (any one)
./venv/bin/python main.py     # directly with project venv
python3 main.py              # any python works: main.py auto-switches to venv
./run.sh                     # one-click launcher (macOS / Linux)
```

> **Self-healing launch**: `main.py` has a built-in venv self-check at the top — if the current interpreter lacks `PyQt6`,
> it auto `re-exec`s into the project's own `venv` interpreter before starting. So running with system `python3`
> will no longer raise `ModuleNotFoundError: No module named 'PyQt6'`.
> If the venv itself is missing PyQt6, install dependencies via step 2 above first.

### Web / Electron app (shared backend)

```bash
# Start the backend (open http://127.0.0.1:8787 in a browser)
PYTHONPATH=. ./venv/bin/python -m api.server                # default port 8787
PYTHONPATH=. ./venv/bin/python -m api.server --port 9000    # custom port
```

## Electron / Web Desktop App

A standalone desktop app packaged with Electron: the main process (`electron/main.js`) starts the Python backend
(`api/server.py`, port 8787) and hosts a `BrowserWindow` loading the frontend under `web/`.
If the backend fails to come up, a dialog is shown and the app exits, avoiding a blank screen. When packaged, Electron embeds the frozen backend executable; in dev it falls back to `venv/bin/python` and the system `python`.

### Launch

```bash
cd electron
npm install        # first time only: install electron + electron-builder
npm start          # start (auto-starts Python backend and opens window, 1280×800)
npm run dev        # dev mode (auto-opens DevTools)
npm run dist:mac   # package macOS installer (dmg)
npm run dist:win   # package Windows installer (nsis)
npm run dist:linux # package Linux installer (AppImage + deb)
```

> If `npm` / Electron download is blocked on your machine, you can also just open `http://127.0.0.1:8787/`
> in any browser (start the backend from the project root first: `PYTHONPATH=. ./venv/bin/python -m api.server`).
> The frontend (`web/`) is the same page loaded inside Electron.

### UI Features

- **Light / dark dual theme**: one-click toggle in the toolbar ("🌓 Theme"); preference persists via `localStorage` and restores on next launch.
- **Branded header**: app mark (🌿) + name "Jira Git GUI" on the left of the toolbar, distinct from a bare web page.
- **Live status dot**: indicator on the left of the bottom status bar — green = credentials configured / yellow = not configured, so backend state is obvious at a glance.
- **Visual polish**: gradient primary buttons, list-item hover lift, soft card shadows, GitHub-style diff tables — modern and cohesive overall.
- **Design system**: `web/styles.css` is driven by CSS variables for color, spacing, radius, and shadow; light / dark is a `body.dark` override, so new components reuse tokens instead of scattering hardcoded values.
- **Tabbed layout**: Repos / File tree / File preview / Commits / Diff / Logs — feature-equivalent to the PyQt version.

## Smart Diff

The diff engine (`core/differ.py`) specifically addresses two common pain points: "same content, different format" and "single-line minified files":

### Line-ending / whitespace filtering

- For text files an extra **normalized hash** is computed (normalize `\r\n` to `\n` first, then MD5).
- When local (e.g. CRLF) and remote (e.g. LF) differ only in line endings / whitespace but are semantically identical, the status is **`line-ending diff` (`WHITESPACE_ONLY`)** — not counted as "modified", and skipped on merge, so the remote style is never polluted.
- The Web "Diff" panel has **"Ignore line-ending differences"** checked by default; unchecking restores "modified" for fine-grained review.

### Structured-file formatting

- For JSON / JSONC / XML families, `canonical_text()` normalizes and expands (JSON `indent=2`, XML `minidom.toprettyxml`) before generating the unified diff.
- Single-line minified files thus become line-level readable diffs — only the actually changed field line is highlighted, instead of the whole line being red.
- **Equality check and merge both use the original bytes**: minified single-line vs pretty multi-line (same content) is still "modified" per original MD5/size; the merge always writes the original remote bytes, never silently "formatting" and polluting the remote.
- Parse failures always return as-is, never raising. Supported: JSON / JSONC / JSON5 / GeoJSON / tfstate / ipynb + XML / XHTML / SVG / WSDL / plist / RSS / Atom / XSL.

## Performance

The diff and merge engine has been hardened over several rounds (measured on repositories with tens of thousands of files):

- **Incremental scanning**: re-scans only changed subtrees, ~**2.7×** faster overall.
- **Set-based diffing**: set operations replace linear per-item comparison, much faster on large repos.
- **Parallel merging**: local write stage processes multiple files in parallel, ~**8×** faster.
- **O(1) file-tree index**: `tree_panel` indexes nodes in a dict; locating / expanding no longer traverses the whole tree.
- **Global token-bucket limiter**: `DEFAULT_REQUEST_QPS=6` in `core/throttle.py` prevents tripping remote rate limits; the Web "merge rate" knob adjusts 15–30 QPS, with automatic backoff on overload.
- **Cache-first**: remote tree / content prefer the lock-guarded JSON cache in `core/cache.py` to avoid re-fetching; `run_merge.py` is likewise cache-first.

> Performance decisions and trade-offs are documented as ADRs and fix reports under `deliverables/gstack/` (e.g. `fix-crlf-whitespace-only-*.md`).

## Configuration File (`.env`)

On startup the app **auto-reads `.env` at the project root** as the default connection config (no need to re-fill "Connection Settings" each time).
The file is gitignored — **do not commit real credentials**. Supported keys (alias- and typo-tolerant):

| `.env` key | Meaning | Notes |
| --- | --- | --- |
| `jira_url` | Jira base URL | also `JIRA_URL` |
| `username` | Account name | for PAT clone, use the PAT owner's account |
| `mode` | Mode | `pat` (default) or `cookie` |
| `personal_access_token` | PAT | also tolerates old spelling `persoanl_access_token` |
| `cookie` | Session cookie | `JSESSIONID=...; atlassian.xsrf.token=...` |

Example:

```ini
jira_url=https://jira.cn
personal_access_token=YOUR_PAT
cookie=JSESSIONID=...; atlassian.xsrf.token=...
```

> Real environment variables (uppercase keys, e.g. `JIRA_URL`) take priority over `.env`, convenient for CI / temporary overrides.
> After frozen packaging, `.env` is searched along both the user data dir `~/.jira-git-gui` and the executable's directory.

## Packaging & Release (Cross-platform)

The tool supports **three release flavors**, all sharing the same Python backend:

| Flavor | Entry | Packaging | Artifact |
|------|------|------|------|
| PyQt6 desktop app | `main.py` | `pyinstaller build/pyinstaller_gui.spec` | `.app` (macOS) / `.exe` (Windows) |
| Web app | Browser | `pyinstaller build/pyinstaller_backend.spec` | single-file backend `jira-git-backend` |
| Electron desktop app | `electron/` | electron-builder (embeds frozen backend) | `.dmg` / `.exe`(nsis) / `.AppImage`+`.deb` |

**Key constraint**: neither PyInstaller nor electron-builder supports **cross-compilation** — each platform's artifact must be built on that OS (or a corresponding CI runner).
A `.github/workflows/release.yml` is configured so pushing a `vX.Y.Z` tag auto-builds on macOS / Windows / Ubuntu runners.

Two key refactors before packaging (already done):

1. **Decouple `core/logger.py` from PyQt6**: made lazy-loaded, so the headless backend is fully free of the GUI framework — ~8 MB after freezing.
2. **Writable runtime dirs**: added `core/app_paths.get_data_root()`, routing logs / cache / downloads / session to `~/.jira-git-gui` (project root in dev) to avoid writing into a read-only frozen bundle.

Detailed local build steps, CI flow, artifact table, and signing notes are in **[docs/PACKAGING.md](docs/PACKAGING.md)**.

## Testing

```bash
# activate the venv that has PyQt6 first
QT_QPA_PLATFORM=offscreen ./venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

Coverage: resume download, client optimizations (binary download / branch cache / parallel download), diff performance & formatting, CRLF / line-ending filtering, token-bucket limiter, file-tree index, commits, Worker exception protection, etc. Integration tests need real credentials and skip automatically when absent.

## Known Limitations

- **Unsigned**: local / CI artifacts are ad-hoc; first open is blocked by Gatekeeper / SmartScreen. Configure a cert for official release.
- **Root `server.py` is deprecated**: it hardcodes an absolute path and is kept only for historical compatibility; new features and packaging use `api/server.py`.
- **Python version**: dev env 3.9 is compatibility-hardened; CI and official packaging recommend **Python ≥ 3.10** (3.11 verified).
- **Linux runtime deps**: the desktop app needs system libs `libgl1` / `libnss3` etc. (installed in CI).
- **YAML formatting**: structured-file diff does not yet include YAML (needs PyYAML); can be extended later.
