# Jira Git GUI

A desktop console for two everyday jobs:

- **Working with Jira Git repositories** — browse files without cloning, compare your local copy against the remote, and merge remote changes back down.
- **Kubernetes day-to-day ops** — read logs, get a shell inside a container, move files, and take one-click health snapshots.

Runs on macOS / Windows / Linux. The same UI ships as an Electron app, a lighter Tauri app, or a plain browser page.

> 中文文档：[README.zh-CN.md](README.zh-CN.md)

## What you can do

| I want to… | Go to |
| --- | --- |
| Browse files and code in a Jira repo | Repository / File tree / Preview |
| Find a file by name, or search inside code | File tree → search box |
| See who changed a directory recently | Diff → Recent updates |
| Compare my local code against the remote | Diff |
| Pull remote changes into my local copy | Diff → Merge |
| See what a file looked like in an older commit | Diff → click a filename in the commit list |
| Read Pod logs and troubleshoot | K8s → Logs |
| Get a shell inside a container | K8s → Shell |
| Download / upload files in a container | K8s → Files |
| Grab status + logs for a batch of Pods | K8s → Snapshot |
| Query cloud-function logs | Cloud functions |

## Getting started

### Option 1 — the packaged app (for everyday use)

Install and run. No Python required.

| Platform | Artifact |
| --- | --- |
| macOS | `.dmg` |
| Windows | `.exe` (NSIS installer) |
| Linux | `.AppImage` / `.deb` |

> Builds are unsigned, so the first launch is blocked by Gatekeeper / SmartScreen. Allow it via System Settings → Privacy & Security (macOS), or "More info → Run anyway" (Windows).

### Option 2 — one-click script (from source)

```bash
./scripts/run.sh              # start the backend and open the browser
./scripts/run.sh --electron   # start the Electron desktop app instead
```

### Option 3 — manual

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
PYTHONPATH=. ./venv/bin/python -m api.server      # then open http://127.0.0.1:8787
```

If `npm` or the Electron download is blocked, skip it — just start the backend and open `http://127.0.0.1:8787/` in any browser. It's the same page the desktop apps load.

### First run: connect to Jira

The app reads `.env` from the project root automatically, so you normally configure this once and never touch the connection dialog again:

| `.env` key | Meaning | Notes |
| --- | --- | --- |
| `jira_url` | Jira base URL | also accepts `JIRA_URL` |
| `username` | Account name | in PAT mode, use the PAT owner's account |
| `mode` | `pat` (default) or `cookie` | see below |
| `personal_access_token` | PAT | also tolerates the typo `persoanl_access_token` |
| `cookie` | Session cookie | `JSESSIONID=...; atlassian.xsrf.token=...` |

`.env` is gitignored — **never commit real credentials**. Real environment variables (e.g. `JIRA_URL`) take priority over `.env`.

**Which mode should I use?**

- **PAT** — clones the repo locally. Most reliable; supports file history and every file type. Needs a personal access token.
- **Cookie** — reads through the Jira web pages. No clone needed, and handy when you can't get a PAT. Trade-offs: a few large/binary files can only be fetched via a fallback, and an expired cookie breaks browsing until you refresh it.

## Workflows

### Browse a repo

1. Pick the repo in the **Repository** tab. The dropdown shows `name · ID`, so repos that share a name are still distinguishable.
2. The **File tree** loads — expand folders, or paste a full path to jump straight to it.
3. Click a file to preview it.

To search, use the file tree's search box: by file name, or inside file contents.

### Sync remote changes into your local copy

The main workflow — think of it as "git, for a repo you can only reach over the web".

1. Open the **Diff** tab.
2. Choose the **compare repo**, then the **compare directory** (type a path, or browse the tree). Narrowing to a subdirectory makes the scan much faster.
3. Leave **Fast scan** on. It compares by file size without downloading anything — on large repos this is the difference between seconds and minutes.
4. Click **Scan**. You get a list of modified / remote-only / local-only / line-ending-only files, with **merged ✓** badges carried over from previous merges.
5. Click **Merge** for a single file, or merge the batch. Progress streams live.
6. Re-running a merge is safe: files already in sync are skipped, and only what's out of date is re-fetched. If you edited a file locally, it gets re-synced rather than silently skipped.

Extras worth knowing:

- **Recent updates** — a git-log-style list of commits touching that directory.
- **Export report** — writes the scan + merge result to Markdown, for archiving or pasting into a Jira comment.
- **Checkbox merge** — tick files in the tree and merge only those.
- **History** — click a filename in the commit list to view that version, and compare it against your local copy.
- **Conflicts** — if both you *and* the remote changed a file since the last sync, the app stops instead of overwriting and asks you to keep local / take remote / merge by hand.

### K8s: read logs

1. **K8s** tab → pick an **environment** (dev / test / prod, colour-coded).
2. Open **Logs** and choose the Pod and container.
3. Search (regex supported), highlight by level, choose how many lines to tail, enable live refresh, or download as `.txt`.

There's also a **full-screen log page** — open it from the snapshot page (⧉) or the Shell, so you can watch several Pods side by side.

### K8s: shell into a container

**K8s → Shell** → pick environment, Pod, container → connect. It's a real terminal: `vim`, `top` and `less` all work, and `cd` persists between commands. History with ↑/↓.

### K8s: move files

**K8s → Files** browses the container filesystem like an FTP client: double-click a text file to edit and save it back, upload, download (with resume and a progress bar for large files), create folders, delete.

### K8s: snapshot

**K8s → Snapshot** runs `kubectl get pods`, grabs logs per Pod, rates each one HIGH / MED / OK, and writes a self-contained HTML report plus JSON into `~/k8s_snapshots/<timestamp>/`. Good for a quick "is anything broken?" pass, or for attaching evidence to a ticket.

### K8s: environments and credentials

Each environment (dev / test / prod) keeps its own kubeconfig, context and default namespace.

Rather than leaving kubeconfigs scattered around (e.g. in `~/Downloads`), use **Environment management → Import kubeconfig**. It copies the file into a controlled directory, `~/.config/jira-git-gui/kubeconfigs/<env>.kubeconfig`, and sets permissions to `600` so only you can read it.

Environments can also be exported for backup or sharing — but the export contains cluster credentials, so move it over an encrypted channel only.

### Cloud-function logs

The **Cloud functions** tab queries, filters, sorts and exports cloud-function logs, and can copy results straight to a file.

## Troubleshooting

| Symptom | Cause / fix |
| --- | --- |
| I changed the code but the UI looks the same | Hard-refresh (**Cmd/Ctrl+Shift+R**). The backend serves the built frontend from disk, so a rebuild needs a refresh, not a restart. |
| "Session expired" banner, or the file tree comes up empty | The Jira cookie expired. Refresh `cookie` in `.env`, or switch to PAT mode. |
| A file won't preview: "too large or binary" | Expected for large/binary files in Cookie mode. Download it from the file tree instead, or use PAT mode. |
| Batch merge fails on a large file | Large/binary files merge through the plugin's raw-file endpoint now. If one still fails, switch to PAT mode. |
| A file shows as modified but the content looks identical | Usually a line-ending difference. "Ignore line-ending differences" is on by default in the Diff panel — untick it to see them. |
| The packaged app behaves differently from the source | The frozen backend is stale. Rebuild it — see [For developers](#for-developers). |
| Same-named repos: the local directory auto-fills wrongly | Known issue — the `.env` `MERGE_REPO_*` mapping is keyed by repo *name*. Pick the repo by the ID shown in the dropdown, and set the local directory manually. |

## What each tab is

- **Repository / File tree / Preview** — pick a repo, browse it, read code.
- **Commits** — search commits by issue or repo, with line-level diffs.
- **Diff** — the compare-and-merge workflow above.
- **K8s** — Snapshot / Pod YAML / Describe / Network / Events / Top / Shell / Files / Logs.
- **Cloud functions** — cloud-function log query and error diagnosis.
- **Log** — the app's own log; useful when something breaks.

The light/dark theme toggle is in the top bar.

## For developers

```bash
# frontend
cd frontend/web-react && npm install
npm run build        # type-check + build; use this, not a bare `vite build`

# tests
QT_QPA_PLATFORM=offscreen ./venv/bin/python -m pytest tests/ --basetemp=.pytest_tmp

# rebuild the frozen backend the desktop apps run
./venv/bin/python build/build.py --flavor backend
```

> **Important:** the desktop apps run a *frozen* backend (`dist/jira-git-backend`). After changing anything under `core/` or `api/`, rebuild it — otherwise the app keeps executing old logic even though your source is new. This has caused real "my fix didn't work" confusion.

Architecture and module layout: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**. Packaging and release: **[docs/PACKAGING.md](docs/PACKAGING.md)**.

## Known limitations

- Builds are **unsigned** — the first launch needs a manual allow.
- **Cookie mode is less capable than PAT** — some large/binary files need a fallback path, and an expired session breaks browsing.
- **One Shell tab = one session.** Disconnecting ends it; for several views at once, use the full-screen log page.
- **`.env` `MERGE_REPO_*` is keyed by repo name** — same-named repos collide, so set the local directory manually for those.
- `main.py`, `gui/` and `workers/` are the older PyQt6 desktop implementation, kept for reference only. The shipped apps are Electron + Tauri.
