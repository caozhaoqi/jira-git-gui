# Jira Git GUI

> 📘 中文文档：[README.zh-CN.md](README.zh-CN.md)

A unified desktop console for **Jira Git Integration** (Xiplink / BigBrassBand) **and Kubernetes daily operations**. It ships in two desktop flavors that share the same Python backend:

- **PyQt6 desktop app** (`main.py`): pure Python + PyQt6, no browser required; all network requests run on background threads so the UI never freezes.
- **Electron / Web app** (`electron/` + `web/`): Electron loads the same Web frontend for cross-platform packaging; you can also just open it in any browser against the local backend.

> Both frontends share the same Python backend (`api/server.py`, default port 8787) and are feature-equivalent.

## Features at a glance

### Jira / Git module

- **Dual auth modes**: switch freely between **PAT** (full `git clone`) and **Cookie** (web fetch / recursive download) authentication.
- **High-performance engine**: incremental scanning (~2.7×), set-based diffing, parallel merging (~8×), O(1) file-tree indexing, and a global token-bucket rate limiter with a UI-adjustable rate.
- **Smart diff**: auto-detects CRLF / LF line-ending and whitespace-only differences (classified as "line-ending diff" instead of "modified"); JSON / JSONC / XML family files are auto-formatted and expanded so single-line minified files become readable line by line.
- **Resume-able downloads**: Cookie mode supports recursive whole-repo downloads (nested files & binaries included) with resume, cancellation, and bounded concurrency (default 4 threads).

### K8s ops module (☸ K8s tab)

| Sub-tab | What it does |
| --- | --- |
| 📸 **Snapshot** | Batch-grab Pod status + logs → severity rating (HIGH / MED / OK) → interactive HTML report + JSON |
| 📝 **Pod YAML** | get / apply any resource (pod / deployment / service / configmap / ingress / statefulset), auto-cleans server-side noise fields (`status`, `managedFields`, `last-applied`, …) before apply |
| 🌐 **Network** | One-click chain check: kubectl → kubeconfig → cluster reachability → intranet TCP probes → internet egress |
| 📡 **Events** | Cluster event stream (warning-first), filter by namespace / object / type, `--all-namespaces`, auto-refresh |
| 📊 **Top** | `kubectl top` — CPU / memory usage bars, Pod ↔ Node scope switch, auto-refresh |
| 💻 **Shell** | Xshell-style interactive terminal inside a Pod container (WebSocket), persistent `cwd`, command history |
| 📁 **Files** | Xftp-style file browser inside a Pod container: list / open-edit / save / upload / download / mkdir / delete |
| 🔍 **Describe** | `kubectl describe` popup with related events, triggered from snapshot, YAML page, or manual input |
| 📜 **Log viewer** | Inline preview + dedicated full-screen page (`log_viewer.html`): search & highlight, level coloring, container & Pod switching, tail lines, live auto-refresh, download |

- **Multi-environment**: dev / test / prod with independent kubeconfigs, color-coded env pills (dev=blue, test=orange, prod=red).
- **Robustness**: kubectl binary auto-located (Homebrew / Docker / system PATH fallback) so the app works even when launched from a GUI with a minimal `PATH`.

## UI Layout

- Sidebar tabs: 仓库 / 文件树 / 文件预览 / 提交记录 / 差异对比 / 日志 / **K8s 快照**
- Light / dark dual theme (one-click toggle, persisted via `localStorage`).
- The global action bar is context-aware — repo-only actions are hidden on the K8s tab.

## Project Structure

```
jira-git-gui/
├── main.py                 # Entry: creates QApplication + MainWindow (PyQt6 desktop app)
├── run_merge.py            # CLI: merge remote repos' latest code into local (cache-first + sync history)
├── k8s_preview.html        # Self-contained demo of the K8s YAML cleaning (no cluster needed)
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
│   ├── errors.py           # Unified exception types
│   ├── k8s_manager.py      # K8s core: env/kubeconfig resolution, YAML get-apply with cleaning,
│   │                       #   events / describe / top, exec & file ops, kubectl auto-location
│   └── k8s_snapshot.py     # Snapshot engine: Pod status + log grabbing, severity, HTML/JSON report
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
│   ├── log_panel.py        # Logs
│   └── k8s_panel.py        # K8s tab (snapshot / YAML / network / events / top / shell / files)
├── workers/                # Async task layer
│   └── tasks.py            # Generic QThread Worker (auto on_log callback; full traceback on error)
├── api/                    # Backend shared by Web / Electron / Tauri
│   └── server.py           # FastAPI: 50+ REST endpoints + SSE push + WebSocket shell, port 8787
├── electron/               # Electron desktop app
│   ├── main.js             # Main process: Python backend lifecycle + BrowserWindow + log bridge
│   ├── preload.js          # Exposes window.electronAPI (contextIsolation isolated)
│   └── package.json        # name / version / start|dev|dist scripts + electron-builder config
├── web/                    # Web frontend (shared by Electron / browser, zero framework deps)
│   ├── index.html          # Page structure (tabs + K8s panes + connection dialog)
│   ├── app.js              # Frontend logic (REST + SSE + WebSocket, pure vanilla JS)
│   ├── styles.css          # Design system (CSS variables, light / dark dual theme)
│   ├── k8s.css             # K8s-specific layout & visuals
│   └── log_viewer.*        # Dedicated full-screen log page (search / highlight / pod & container switch)
├── tauri/                  # Tauri shell (optional 3rd desktop flavor)
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

> **Self-healing launch**: `main.py` has a built-in venv self-check — if the current interpreter lacks `PyQt6`, it auto re-execs into the project's own `venv` interpreter before starting.

### Web / Electron app (shared backend)

```bash
# Start the backend (open http://127.0.0.1:8787 in a browser)
PYTHONPATH=. ./venv/bin/python -m api.server                # default port 8787
PYTHONPATH=. ./venv/bin/python -m api.server --port 9000    # custom port
```

```bash
cd electron
npm install        # first time only
npm start          # start (auto-starts Python backend and opens the window)
npm run dev        # dev mode (auto-opens DevTools)
npm run dist:mac   # package macOS installer (dmg)
npm run dist:win   # package Windows installer (nsis)
npm run dist:linux # package Linux installer (AppImage + deb)
```

> If `npm` / Electron download is blocked on your machine, just start the backend and open `http://127.0.0.1:8787/` in any browser — the frontend is the same page Electron loads.

## K8s Ops Module

### Environment management

Environments (dev / test / prod …) are stored in `~/.config/jira-git-gui/k8s_envs.json`, each with its own kubeconfig path, optional context and default namespace. The env picker in the K8s tab shows a color-coded pill, and the **YAML** / **Events** / **Top** panes auto-refresh their lists when you switch environments.

### Log viewer (`web/log_viewer.html`)

Open from the snapshot log panel ("⧉ open in new page") or directly:

```
http://127.0.0.1:8787/web/log_viewer.html?pod=<pod>&env=<env>
```

- **Pod switching**: pick any Pod from the top dropdown — logs, containers and namespace switch automatically.
- **Container switching** for multi-container Pods; `--previous` support for restarted containers.
- **Search**: keyword (regex supported), ignore-case, match counter `N/M`, ▲▼ jump between matches.
- **Level highlighting**: ERROR/FATAL red, WARN yellow, DEBUG dimmed.
- **Tail lines**: 50 / 200 / 500 / 1000 / full (5000).
- **Live tail**: auto-refresh every 3 / 5 / 10 s with follow-bottom.
- **Line numbers, wrap toggle, font size ±, download as `.txt`, theme toggle**.

### Shell & files (Xshell / Xftp style)

- **Shell**: WebSocket terminal into a Pod container (`/ws/k8s/exec`). Choose env → pod → container → connect; run commands with persistent working directory (`cd` survives across commands), command history via ↑/↓.
- **Files**: browse the container filesystem with breadcrumbs; double-click a text file to edit inline and save back; upload (base64), download, mkdir, delete with confirmation.

### Snapshot report

`kubectl get pods` + per-Pod log grabbing → severity rating (HIGH / MED / OK) → a self-contained HTML report plus `pods.json` / `summary.json` under `~/k8s_snapshots/<timestamp>/`. Pods with abnormal status get their logs saved on disk; the in-app log panel falls back to live cluster fetching when a snapshot log is missing.

## Smart Diff

The diff engine (`core/differ.py`) addresses two common pain points: "same content, different format" and "single-line minified files".

### Line-ending / whitespace filtering

- A normalized hash (CRLF → LF, then MD5) is computed for text files.
- When local (e.g. CRLF) and remote (e.g. LF) differ only in line endings / whitespace, the status is **`line-ending diff`** — not counted as "modified" and skipped on merge.
- The Web Diff panel has "Ignore line-ending differences" checked by default; unchecking restores "modified".

### Structured-file formatting

- JSON / JSONC / XML families are normalized and expanded (`indent=2` / `toprettyxml`) before diffing, so single-line minified files become line-level readable diffs.
- Equality checks and merges always use the **original bytes** — the remote style is never "prettified" into the remote repo.
- Parse failures return as-is, never raising. Supported: JSON / JSONC / JSON5 / GeoJSON / tfstate / ipynb + XML / XHTML / SVG / WSDL / plist / RSS / Atom / XSL.

## Performance

Measured on repositories with tens of thousands of files:

- **Incremental scanning**: re-scans only changed subtrees, ~**2.7×** faster.
- **Set-based diffing**: set operations replace linear per-item comparison.
- **Parallel merging**: ~**8×** faster local write stage.
- **O(1) file-tree index**: dict-based node index, no full-tree traversal.
- **Token-bucket limiter**: `DEFAULT_REQUEST_QPS=6` in `core/throttle.py`; the Web merge-rate knob adjusts 15–30 QPS with automatic backoff.
- **Cache-first**: lock-guarded JSON cache in `core/cache.py` avoids re-fetching remote trees / content.

## Configuration (`.env`)

The app **auto-reads `.env` at the project root** as the default connection config. The file is gitignored — **do not commit real credentials**. Supported keys (alias- and typo-tolerant):

| `.env` key | Meaning | Notes |
| --- | --- | --- |
| `jira_url` | Jira base URL | also `JIRA_URL` |
| `username` | Account name | for PAT clone, use the PAT owner's account |
| `mode` | Mode | `pat` (default) or `cookie` |
| `personal_access_token` | PAT | also tolerates `persoanl_access_token` |
| `cookie` | Session cookie | `JSESSIONID=...; atlassian.xsrf.token=...` |

> Real environment variables (uppercase keys, e.g. `JIRA_URL`) take priority over `.env`. After frozen packaging, `.env` is searched in both `~/.jira-git-gui` and the executable's directory.

## Packaging & Release (Cross-platform)

Three release flavors, all sharing the same Python backend:

| Flavor | Entry | Packaging | Artifact |
| --- | --- | --- | --- |
| PyQt6 desktop app | `main.py` | `pyinstaller build/pyinstaller_gui.spec` | `.app` (macOS) / `.exe` (Windows) |
| Web app | Browser | `pyinstaller build/pyinstaller_backend.spec` | single-file backend `jira-git-backend` |
| Electron desktop app | `electron/` | electron-builder (embeds frozen backend) | `.dmg` / `.exe` (nsis) / `.AppImage`+`.deb` |

**Key constraint**: neither PyInstaller nor electron-builder supports cross-compilation — each platform's artifact must be built on that OS. A `.github/workflows/release.yml` auto-builds on macOS / Windows / Ubuntu runners when a `vX.Y.Z` tag is pushed.

Detailed local build steps, CI flow, artifact table, and signing notes are in **[docs/PACKAGING.md](docs/PACKAGING.md)**.

## Testing

```bash
QT_QPA_PLATFORM=offscreen ./venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

Coverage: resume download, client optimizations (binary download / branch cache / parallel download), diff performance & formatting, CRLF / line-ending filtering, token-bucket limiter, file-tree index, commits, Worker exception protection, K8s YAML cleaning, `kubectl top` parsing, exec `cwd` tracking, `ls -la` parsing, file write (text & binary base64 path). Integration tests need real credentials / clusters and skip automatically when absent.

## Known Limitations

- **Unsigned**: local / CI artifacts are ad-hoc; first open is blocked by Gatekeeper / SmartScreen. Configure a cert for official release.
- **Root `server.py` is deprecated**: hardcoded absolute path, kept only for historical compatibility; new features use `api/server.py`.
- **Python version**: dev env 3.9 is compatibility-hardened; CI and official packaging recommend **Python ≥ 3.10** (3.11 verified).
- **Linux runtime deps**: the desktop app needs system libs `libgl1` / `libnss3` etc.
- **K8s shell is non-TTY**: commands run via `sh -c` pipes (no interactive editor / `top` full-screen); interactive terminals are a P2 roadmap item.
