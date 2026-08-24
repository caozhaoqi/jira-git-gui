# Jira Git GUI

> 📘 中文文档：[README.zh-CN.md](README.zh-CN.md)

A unified desktop console for **Jira Git Integration** (Xiplink / BigBrassBand) **and Kubernetes daily operations**. It ships in **two desktop flavors** that share the same Python backend and vanilla web frontend:

- **Electron** (`electron/` + `web/`): cross-platform desktop app built on Electron + a Chromium/WebKit webview.
- **Tauri** (`tauri/` + `web/`): lightweight desktop app using the OS-native WebView — far smaller bundle (tens of MB vs. hundreds of MB).

Both load the same web frontend (built from `web-react/`) against the shared Python backend (`api/server.py`, default port 8787) and are feature-equivalent. The Python backend (FastAPI) is bundled into each desktop app, so no separate server process or browser is required by end users.

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
| 📜 **Log viewer** | Inline preview + dedicated full-screen page (`?view=log`): search & highlight, level coloring, container & Pod switching, tail lines, live auto-refresh, download |

- **Multi-environment**: dev / test / prod with independent kubeconfigs, color-coded env pills (dev=blue, test=orange, prod=red).
- **Robustness**: kubectl binary auto-located (Homebrew / Docker / system PATH fallback) so the app works even when launched from a GUI with a minimal `PATH`.

## UI Layout

- Sidebar tabs: Repository / File tree / File preview / Commits / Diff / Log / **K8s Snapshot**
- Light / dark dual theme (one-click toggle, persisted via `localStorage`).
- The global action bar is context-aware — repo-only actions are hidden on the K8s tab.

## Project Structure

```
jira-git-gui/
├── run_merge.py            # CLI: merge remote repos' latest code into local (cache-first + sync history)
├── scripts/                # Launchers & build scripts (cross-platform: *.sh + *.ps1)
├── config/                 # Local config JSON (cf_accounts.*, hcm_whitelist.*) — see .gitignore
│                           # 安全说明：所有含真实凭证/IP/域名的连接信息只存在于 `*.local.json`
│                           # （如 config/hcm_whitelist.local.json、config/cf_accounts.local.json），
│                           # 这些文件已被 .gitignore 忽略、永不入库；仓库内仅保留占位模板。
├── tools/k8s_preview.html  # Self-contained demo of the K8s YAML cleaning (no cluster needed)
├── requirements.txt        # fastapi / uvicorn / httpx / pyinstaller
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
│   ├── logger.py           # Rotating file log + LogBridge (UI bridge) + global excepthook
│   ├── safe.py             # safe_slot decorator: catches slot exceptions, prevents UI crashes
│   ├── errors.py           # Unified exception types
│   ├── k8s_manager.py      # K8s core: env/kubeconfig resolution, YAML get-apply with cleaning,
│   │                       #   events / describe / top, exec & file ops, kubectl auto-location
│   └── k8s_snapshot.py     # Snapshot engine: Pod status + log grabbing, severity, HTML/JSON report
├── api/                    # Python backend, shared by Web / Electron / Tauri
│   └── server.py           # FastAPI: 50+ REST endpoints + SSE push + WebSocket shell, port 8787
├── web/                    # Web frontend production build (generated by `vite build` in web-react/):
│                          #   index.html + assets/ — shared by Electron / Tauri / browser.
│                          #   The original vanilla-JS version is archived under web-legacy/ for rollback.
├── web-react/             # React + TypeScript + Vite frontend — the live source of web/
│   ├── src/components/    # Feature panels: RepoPanel / CommitsPanel / DiffPanel / k8s/* / CfPanel
│   ├── src/api/           # Unified API client, SSE event manager, typed models
│   ├── src/store/         # Zustand global store (logs / toasts / progress / activeTab)
│   └── src/utils/         # format (diff / relative-time / size) + clipboard (Electron/Tauri/Web)
├── electron/               # Electron desktop app (released)
│   ├── main.js             # Main process: Python backend lifecycle + BrowserWindow + log bridge
│   ├── preload.js          # Exposes window.electronAPI (contextIsolation isolated)
│   └── package.json        # name / version / start|dev|dist scripts + electron-builder config
├── tauri/                  # Tauri desktop app (released)
│   └── src-tauri/          # Rust shell: Python backend lifecycle + WebView window
├── build/                  # PyInstaller spec for the frozen backend (shared by Electron & Tauri)
├── tests/                  # Unit tests (unit before integration, version-controlled)
├── store/                  # Runtime artifacts (git clone / downloads, gitignored)
├── logs/                   # Runtime logs (full traceback, gitignored)
└── docs/
    └── PACKAGING.md        # Packaging & cross-platform release details
```

> **Legacy note**: `main.py`, `gui/` and `workers/` contain the older PyQt6 desktop implementation. They are kept for reference / local development but are **not** part of the published releases (which are Electron + Tauri). The deprecated root `server.py` is also retained only for historical compatibility; all released builds use `api/server.py`.

Dependency direction: `gui → workers → core`; `core` does not depend back on GUI, so it can be reused and tested in isolation.

## React Frontend (`web-react/`)

`web/` is the production build of the React + TypeScript + Vite frontend in `web-react/`. The migration from the original vanilla-JS frontend is **complete**: `web/` now ships the React bundle (the old vanilla version is archived in `web-legacy/` for rollback). All feature blocks are ported with behaviour-parity:

| Tab | Component | What it does |
| --- | --- | --- |
| Repository / Files | `RepoPanel` | Repo list / file tree / preview (first block, migrated earlier) |
| Commits | `CommitsPanel` | Query commits by Issue / local repo, GitHub-style list + line-level diff |
| Diff | `DiffPanel` | Local↔remote diff scan, GitHub-style diff, single / batch merge (with SSE progress) |
| K8s Ops | `k8s/K8sPanel` | Snapshot / Pod YAML / **Describe** / Network / Events / Top / **Shell terminal** / Files / **full-screen log viewer** |
| CF Logs | `CfPanel` | Cloud-function log query / sort / search / export / clipboard-to-file |

Notes:

- **Describe** (`k8s/K8sDescribeModal`): a `kubectl describe` popup with related events, triggered from the Snapshot or YAML page; corresponds to the vanilla `openK8sDescribe`.
- **Full-screen log viewer** (`LogViewer`, `?view=log`): Pod/container switching, search highlight, level highlight, tail lines, `--previous`, auto-refresh (live tail), download. Opened in a new window via `utils/logviewer.ts`, preserving the ability to open multiple Pods at once; corresponds to the vanilla `web/log_viewer.html`.

Conventions carried over from the vanilla frontend:

- **State**: Zustand global store (`useAppStore`) for logs / toasts / progress / active tab.
- **API**: unified client (`apiGet` / `apiPost` / `apiDelete`) that mirrors `web/js/01-core.js` error classification.
- **SSE**: typed event manager (`sse.on(event, handler)`); new event names must be registered in `SSEEventMap` (`src/api/types.ts`). Diff scan/merge and K8s snapshot progress are pushed over SSE.
- **Shell**: xterm.js terminal (`@xterm/xterm`) over the `/ws/k8s/exec` WebSocket, with persistent `cwd` and ↑/↓ command history.
- **Styles**: split by responsibility into `src/styles/global.css` (shell: topbar / modal / tree / log panel) + `src/styles/panels.css` (commits/diff/k8s/cf panels, class names aligned with vanilla `web/styles.css` + `web/k8s.css`) + `src/styles/logviewer.css` (full-screen log viewer). `panels.css` is extracted from vanilla CSS and remapped to React design tokens, avoiding two-way conflicts across 140 same-named classes.
- **Local config**: CF accounts etc. are read from `config/cf_accounts.local.json` (gitignored); the frontend only shows environment names and never renders credentials.

### Develop / build

```bash
cd web-react
npm install        # first time only
npm run dev        # Vite dev server (hot reload)
npm run typecheck  # tsc --noEmit
npm run build      # type-check + build to dist/ (base=/web/, served by the backend at /web/)
npm run preview    # preview the built bundle
```

> The backend serves `web-react/dist` first (the `vite build --base /web/` output), falling back to `web/` when absent; both are mounted under `/web/`. `node_modules/` is gitignored. `web/` and `web-react/dist` are identical (synced at release); both are React build artifacts.

## Running

### Web / Electron app (shared backend)

```bash
# 1. Create and activate a venv (skip if venv already exists)
python3 -m venv venv
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the backend (open http://127.0.0.1:8787 in a browser)
PYTHONPATH=. ./venv/bin/python -m api.server                # default port 8787
PYTHONPATH=. ./venv/bin/python -m api.server --port 9000    # custom port

# Or use the one-click launcher
./scripts/run.sh             # Web backend + opens browser (macOS / Linux / Windows-Git-Bash)
./scripts/run_web.sh         # same as above
./scripts/run_web.sh --electron   # launch the Electron desktop app instead
```

If `npm` / Electron download is blocked on your machine, just start the backend and open `http://127.0.0.1:8787/` in any browser — the frontend is the same page Electron / Tauri load.

### Electron desktop app

```bash
cd electron
npm install        # first time only
npm start          # start (auto-starts Python backend and opens the window)
npm run dev        # dev mode (auto-opens DevTools)
npm run dist:mac   # package macOS installer (dmg)
npm run dist:win   # package Windows installer (nsis)
npm run dist:linux # package Linux installer (AppImage + deb)
```

### Tauri desktop app

```bash
# Requires Rust (https://rustup.rs) and system WebView dev libs
./scripts/build-tauri.sh            # release build → .app / .dmg (macOS), .msi (Windows), .AppImage+.deb (Linux)
cargo tauri dev                     # live dev (hot-reload) from tauri/

# Windows (PowerShell)
.\scripts\build-tauri.ps1
```

> The Tauri build embeds the same frozen Python backend, so no separate server is needed at runtime.

## K8s Ops Module

### Environment management

Environments (dev / test / prod …) are stored in `~/.config/jira-git-gui/k8s_envs.json`, each with its own kubeconfig path, optional context and default namespace. The env picker in the K8s tab shows a color-coded pill, and the **YAML** / **Events** / **Top** panes auto-refresh their lists when you switch environments.

### Log viewer (`/web/?view=log`)

Open from the snapshot page (the "open full log in new page" button ⧉) or the K8s Shell, or directly (port is the actual runtime port, default 8787):

```
/web/?view=log&pod=<pod>&env=<env>&container=<container>&namespace=<namespace>
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
| `cookie` | Session cookie | format: `JSESSIONID=...; atlassian.xsrf.token=...` |

> Real environment variables (uppercase keys, e.g. `JIRA_URL`) take priority over `.env`. After frozen packaging, `.env` is searched in both `~/.jira-git-gui` and the executable's directory.

## Packaging & Release (Cross-platform)

Two released desktop flavors, both embedding the same frozen Python backend:

| Flavor | Entry | Packaging | Artifact |
| --- | --- | --- | --- |
| Electron desktop app | `electron/` | electron-builder (embeds frozen backend) | `.dmg` (macOS) / `.exe` (Windows, nsis) / `.AppImage`+`.deb` (Linux) |
| Tauri desktop app | `tauri/` | `cargo tauri build` (embeds frozen backend) | `.app`+`.dmg` (macOS) / `.msi` (Windows) / `.AppImage`+`.deb` (Linux) |

**Key constraint**: neither electron-builder nor `cargo tauri` supports cross-compilation — each platform's artifact must be built on that OS. A `.github/workflows/release.yml` auto-builds on macOS / Windows / Ubuntu runners when a `vX.Y.Z` tag is pushed.

Detailed local build steps, CI flow, artifact table, and signing notes are in **[docs/PACKAGING.md](docs/PACKAGING.md)**.

## Testing

```bash
QT_QPA_PLATFORM=offscreen ./venv/bin/python -m unittest discover -s tests -p "test_*.py"
```

Coverage: resume download, client optimizations (binary download / branch cache / parallel download), diff performance & formatting, CRLF / line-ending filtering, token-bucket limiter, file-tree index, commits, Worker exception protection, K8s YAML cleaning, `kubectl top` parsing, exec `cwd` tracking, `ls -la` parsing, file write (text & binary base64 path). Integration tests need real credentials / clusters and skip automatically when absent.

## Known Limitations

- **Unsigned**: local / CI artifacts are ad-hoc; first open is blocked by Gatekeeper / SmartScreen. Configure a cert for official release.
- **Root `server.py` is deprecated**: hardcoded absolute path, kept only for historical compatibility; new features use `api/server.py`.
- **PyQt6 desktop (`main.py` / `gui/`) is legacy**: no longer a release target; the shipped apps are Electron + Tauri.
- **Python version**: dev env 3.9 is compatibility-hardened; CI and official packaging recommend **Python ≥ 3.10** (3.11 verified).
- **Linux runtime deps**: the desktop apps need system libs `libnss3` (Electron) / WebView dev libs (Tauri) etc.
- **K8s shell session is single-connection TTY**: one Shell tab maps to one `kubectl exec -it` session; disconnecting ends it. For multiple concurrent views use the standalone log viewer window.
