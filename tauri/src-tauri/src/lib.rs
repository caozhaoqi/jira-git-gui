use chrono::Local;
use serde::Serialize;
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::time::Duration;
use tauri::Emitter;
use tauri::Manager;

// ---------------------------------------------------------------------------
//  Constants
// ---------------------------------------------------------------------------
/// 后端首选端口；被占用时自动向后探测（见 pick_backend_port）。
const BACKEND_PORT: u16 = 8787;
/// 端口被占用时最多向后尝试的候选个数。
const PORT_SCAN_RANGE: u16 = 20;

// ---------------------------------------------------------------------------
//  State
// ---------------------------------------------------------------------------
struct BackendProcess(Mutex<Option<Child>>);

/// 实际生效的后端端口（启动时探测确定，供 IPC 查询）。
struct BackendPort(u16);

// ---------------------------------------------------------------------------
//  Port helpers
// ---------------------------------------------------------------------------
fn is_port_free(port: u16) -> bool {
    std::net::TcpListener::bind(("127.0.0.1", port)).is_ok()
}

/// 从 BACKEND_PORT 起找第一个空闲端口；范围内全被占用则返回 None。
fn pick_backend_port() -> Option<u16> {
    (BACKEND_PORT..BACKEND_PORT.saturating_add(PORT_SCAN_RANGE)).find(|p| is_port_free(*p))
}

fn backend_origin(port: u16) -> String {
    format!("http://127.0.0.1:{}", port)
}

// ---------------------------------------------------------------------------
//  Path helpers (mirrors electron/main.js)
// ---------------------------------------------------------------------------
/// 定位项目根目录（= 仓库根，含 web/、api/、venv/ 等）。
/// CARGO_MANIFEST_DIR = <项目根>/tauri/src-tauri，向上两级即项目根。
/// 用 canonicalize 规范化，避免软链接/相对路径导致层级偏差。
fn project_root() -> PathBuf {
    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    std::fs::canonicalize(
        manifest
            .parent() // src-tauri
            .unwrap()
            .parent() // tauri
            .unwrap(),
    )
    .unwrap_or_else(|_| manifest.parent().unwrap().parent().unwrap().to_path_buf())
}

fn data_dir() -> PathBuf {
    if cfg!(debug_assertions) {
        project_root()
    } else {
        dirs_next_home().join(".jira-git-gui")
    }
}

fn dirs_next_home() -> PathBuf {
    #[cfg(target_os = "windows")]
    {
        std::env::var("USERPROFILE").map(PathBuf::from).unwrap_or_else(|_| PathBuf::from("."))
    }
    #[cfg(not(target_os = "windows"))]
    {
        std::env::var("HOME").map(PathBuf::from).unwrap_or_else(|_| PathBuf::from("."))
    }
}

fn log_file_path(data_dir: &PathBuf) -> PathBuf {
    let log_dir = data_dir.join("logs");
    fs::create_dir_all(&log_dir).ok();
    let date = Local::now().format("%Y%m%d").to_string();
    log_dir.join(format!("electron-{}.log", date))
}

fn timestamp() -> String {
    Local::now().format("%H:%M:%S.%3f").to_string()
}

// ---------------------------------------------------------------------------
//  Logging (matches Electron format: HH:MM:SS.mmm [tag] [level] msg)
// ---------------------------------------------------------------------------
fn log_raw(app_handle: &tauri::AppHandle, log_file: &PathBuf, line: &str) {
    let text = format!("{} {}", timestamp(), line);
    println!("{}", text);
    if let Ok(mut f) = fs::OpenOptions::new().append(true).create(true).open(log_file) {
        let _ = writeln!(f, "{}", text);
    }
    let _ = app_handle.emit("log:append", serde_json::json!({"text": text}));
}

fn log_main(app_handle: &tauri::AppHandle, log_file: &PathBuf, msg: &str, level: &str) {
    log_raw(app_handle, log_file, &format!("[main] [{}] {}", level, msg));
}

// ---------------------------------------------------------------------------
//  Backend process management
// ---------------------------------------------------------------------------
/// 定位开发态使用的 Python 解释器：优先项目 venv（按平台区分目录布局），
/// venv 不存在时回退到 PATH 上的 python3 / python，避免直接 spawn 失败。
fn dev_python(project_root: &PathBuf) -> PathBuf {
    let venv = project_root.join("venv");
    let candidates: [PathBuf; 2] = [
        venv.join("bin").join("python"),          // macOS / Linux
        venv.join("Scripts").join("python.exe"),  // Windows
    ];
    for c in candidates.iter() {
        if c.exists() {
            return c.clone();
        }
    }
    if cfg!(target_os = "windows") {
        PathBuf::from("python")
    } else {
        PathBuf::from("python3")
    }
}

fn get_backend_launch(
    project_root: &PathBuf,
    resource_dir: &PathBuf,
    port: u16,
) -> (PathBuf, Vec<String>) {
    let port = port.to_string();
    if cfg!(debug_assertions) {
        (
            dev_python(project_root),
            vec!["-m".into(), "api.server".into(), "--port".into(), port],
        )
    } else {
        // Production: use bundled backend binary from Tauri resource directory
        let exe_name = if cfg!(target_os = "windows") {
            "jira-git-backend.exe"
        } else {
            "jira-git-backend"
        };
        let cmd = resource_dir.join("backend").join(exe_name);
        (cmd, vec!["--port".into(), port])
    }
}

fn start_backend(
    project_root: &PathBuf,
    data_dir: &PathBuf,
    resource_dir: &PathBuf,
    port: u16,
) -> std::io::Result<Child> {
    let (cmd, args) = get_backend_launch(project_root, resource_dir, port);

    Command::new(&cmd)
        .args(&args)
        .current_dir(data_dir)  // 生产模式下用数据目录作为工作目录
        .env("JIRA_GIT_DATA_DIR", data_dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
}

async fn wait_for_backend(max_retries: u32, port: u16) -> Result<(), String> {
    let client = reqwest::Client::new();
    let url = format!("{}/api/status", backend_origin(port));

    for i in 0..max_retries {
        let result = client
            .get(url.as_str())
            .timeout(Duration::from_millis(800))
            .send()
            .await;

        match result {
            Ok(resp) if resp.status().is_success() => {
                return Ok(());
            }
            Ok(resp) => {
                log::info!("Backend returned HTTP {} (retry {})", resp.status(), i);
            }
            Err(e) => {
                if i == 0 || i % 5 == 4 {
                    log::info!("Backend not reachable (retry {}, reason: {})", i, e);
                }
            }
        }

        tokio::time::sleep(Duration::from_millis(500)).await;
    }

    Err(format!(
        "Backend not ready after {} retries (~{} seconds)",
        max_retries,
        (max_retries as f64 * 0.5) as u32
    ))
}

/// Spawn threads that read child stdout/stderr and forward to the log system.
fn pipe_child_output(
    child: &mut Child,
    app_handle: tauri::AppHandle,
    log_file: PathBuf,
) {
    // stdout
    if let Some(stdout) = child.stdout.take() {
        let h = app_handle.clone();
        let lf = log_file.clone();
        std::thread::spawn(move || {
            let reader = BufReader::new(stdout);
            for line in reader.lines() {
                match line {
                    Ok(text) => {
                        if !text.trim().is_empty() {
                            log_raw(&h, &lf, &format!("[py:out] {}", text));
                        }
                    }
                    Err(_) => break,
                }
            }
        });
    }

    // stderr
    if let Some(stderr) = child.stderr.take() {
        let h = app_handle.clone();
        let lf = log_file.clone();
        std::thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines() {
                match line {
                    Ok(text) => {
                        if !text.trim().is_empty() {
                            log_raw(&h, &lf, &format!("[py:err] {}", text));
                        }
                    }
                    Err(_) => break,
                }
            }
        });
    }
}

// ---------------------------------------------------------------------------
//  Backend shutdown
// ---------------------------------------------------------------------------
/// 优雅关闭 Python 后端：Unix 先送 SIGTERM 等其自行退出，超时再 SIGKILL；
/// Windows 直接 TerminateProcess。对齐 Electron 的 pyProc.kill('SIGTERM') 行为，
/// 避免关闭窗口后 Python 进程变成孤儿进程继续占用端口。
fn kill_backend(app_handle: &tauri::AppHandle) {
    let Some(state) = app_handle.try_state::<BackendProcess>() else {
        return;
    };
    let Ok(mut guard) = state.0.lock() else {
        return;
    };
    let Some(mut child) = guard.take() else {
        return; // 已经清理过，避免重复 kill
    };

    let pid = child.id();

    #[cfg(unix)]
    {
        // SIGTERM：让 uvicorn 执行 shutdown 钩子，落盘未写完的日志/缓存
        unsafe {
            libc::kill(pid as libc::pid_t, libc::SIGTERM);
        }
        for _ in 0..20 {
            match child.try_wait() {
                Ok(Some(_)) => {
                    println!("Backend exited after SIGTERM (pid={})", pid);
                    return;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                Err(_) => break,
            }
        }
        println!("Backend ignored SIGTERM, sending SIGKILL (pid={})", pid);
    }

    let _ = child.kill();
    let _ = child.wait();
    println!("Backend terminated (pid={})", pid);
}

// ---------------------------------------------------------------------------
//  Tauri commands (IPC → frontend)
// ---------------------------------------------------------------------------

#[derive(Serialize)]
struct AppInfo {
    platform: String,
    is_tauri: bool,
    backend_url: String,
    log_file: String,
    is_dev: bool,
}

#[tauri::command]
fn get_app_info(
    state: tauri::State<'_, LogFilePath>,
    port: tauri::State<'_, BackendPort>,
) -> AppInfo {
    AppInfo {
        platform: std::env::consts::OS.to_string(),
        is_tauri: true,
        backend_url: backend_origin(port.0),
        log_file: state.0.to_string_lossy().to_string(),
        is_dev: cfg!(debug_assertions),
    }
}

#[tauri::command]
fn log_message(
    level: String,
    msg: String,
    app_handle: tauri::AppHandle,
    state: tauri::State<'_, LogFilePath>,
) {
    log_raw(&app_handle, &state.0, &format!("[renderer] [{}] {}", level, msg));
}

struct LogFilePath(PathBuf);

// ---------------------------------------------------------------------------
//  App entry point
// ---------------------------------------------------------------------------
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let project_root = project_root();
    let data_dir = data_dir();
    let log_file = log_file_path(&data_dir);

    log::info!("========== Jira Git GUI (Tauri) 启动 ==========");
    log::info!(
        "Platform: {}  Rust: {}",
        std::env::consts::OS,
        env!("CARGO_PKG_RUST_VERSION")
    );
    log::info!("Project root: {}", project_root.display());
    log::info!("Log file: {}", log_file.display());

    // 端口探测：首选 8787，被占用则向后顺延，避免"端口已被占用"直接启动失败
    // （常见于上次异常退出留下的残留后端，或用户手动跑了 run_web.sh）。
    let port = match pick_backend_port() {
        Some(p) => p,
        None => {
            log::error!(
                "No free port in range {}..{}, aborting.",
                BACKEND_PORT,
                BACKEND_PORT + PORT_SCAN_RANGE
            );
            eprintln!(
                "启动失败：端口 {}..{} 全部被占用，请先关闭占用这些端口的进程。",
                BACKEND_PORT,
                BACKEND_PORT + PORT_SCAN_RANGE
            );
            std::process::exit(1);
        }
    };
    if port != BACKEND_PORT {
        log::warn!("Port {} busy, falling back to {}", BACKEND_PORT, port);
    }
    log::info!("Backend URL: {}", backend_origin(port));

    tauri::Builder::default()
        .plugin(tauri_plugin_clipboard_manager::init())
        // 单实例锁：第二次启动时聚焦已有窗口并退出新实例，
        // 避免双份后端进程/双份日志写入同一数据目录。
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            // log::info! 在未初始化 logger 时是 no-op，改用项目的 log_main 落盘
            if let Some(ls) = app.try_state::<LogFilePath>() {
                log_main(app, &ls.0, "second-instance: focusing existing window", "info");
            }
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.unminimize();
                let _ = window.set_focus();
            }
        }))
        .manage(LogFilePath(log_file.clone()))
        .manage(BackendPort(port))
        .setup(move |app| {
            let app_handle = app.handle().clone();

            // SIGTERM / SIGINT 兜底：外部 kill（如系统关机、进程管理工具）时
            // 走正常退出流程，让 RunEvent::Exit 的清理逻辑得以执行。
            #[cfg(unix)]
            {
                let h = app_handle.clone();
                tauri::async_runtime::spawn(async move {
                    use tokio::signal::unix::{signal, SignalKind};
                    let mut term = match signal(SignalKind::terminate()) {
                        Ok(s) => s,
                        Err(_) => return,
                    };
                    let mut int = match signal(SignalKind::interrupt()) {
                        Ok(s) => s,
                        Err(_) => return,
                    };
                    tokio::select! {
                        _ = term.recv() => {}
                        _ = int.recv() => {}
                    }
                    log::info!("Received SIGTERM/SIGINT, exiting gracefully.");
                    h.exit(0);
                });
            }

            // Resolve resource directory for backend binary
            let resource_dir = app
                .path()
                .resource_dir()
                .unwrap_or_else(|_| project_root.clone());

            // ---- Launch Python backend ----
            log_main(&app_handle, &log_file, "Launching Python backend...", "info");

            let mut child = match start_backend(&project_root, &data_dir, &resource_dir, port) {
                Ok(c) => {
                    log_main(
                        &app_handle,
                        &log_file,
                        &format!("Python pid={}", c.id()),
                        "info",
                    );
                    c
                }
                Err(e) => {
	                    let msg = format!("Failed to start Python backend: {}", e);
	                    log_main(&app_handle, &log_file, &msg, "error");
	                    app_handle.exit(1);
	                    return Ok(());
	                }
            };

            // Pipe child's stdout/stderr to log system
            pipe_child_output(&mut child, app_handle.clone(), log_file.clone());

            // Store child process for cleanup
            app.manage(BackendProcess(Mutex::new(Some(child))));

            // ---- Wait for backend to be ready ----
            log_main(&app_handle, &log_file, "Waiting for backend...", "info");

            let timeout_ms = 15000;
            let wait_result = tauri::async_runtime::block_on(async {
                tokio::time::timeout(
                    Duration::from_millis(timeout_ms),
                    wait_for_backend(30, port),
                )
                .await
            });

            match wait_result {
                Ok(Ok(())) => {
                    log_main(&app_handle, &log_file, "Backend ready, creating window.", "info");
                }
                Ok(Err(e)) => {
                    log_main(&app_handle, &log_file, &e, "error");
                    log::error!(
                        "Backend startup failed: {}\nLog file: {}",
                        e,
                        log_file.display()
                    );
                    app_handle.exit(1);
                    return Ok(());
                }
                Err(_timeout) => {
                    let msg = format!(
                        "Backend startup timed out after {} seconds",
                        timeout_ms / 1000
                    );
                    log_main(&app_handle, &log_file, &msg, "error");
                    log::error!(
                        "Backend startup failed: {}\nLog file: {}",
                        msg,
                        log_file.display()
                    );
                    app_handle.exit(1);
                    return Ok(());
                }
            }

            // ---- Create window ----
            // 始终加载后端 URL，由 Python 后端提供前端（与 Electron 行为一致）。
            // 端口来自启动时探测结果，可能是顺延后的备用端口。
            let url = tauri::WebviewUrl::External(
                backend_origin(port).parse().unwrap(),
            );

            tauri::WebviewWindowBuilder::new(app, "main", url)
                .title("Jira Git GUI")
                .inner_size(1280.0, 800.0)
                .min_inner_size(900.0, 600.0)
                .build()?;

            log_main(&app_handle, &log_file, "Window created.", "info");

            // ---- 进程守卫：后端意外退出（OOM / 异常 / 崩溃）时自动重启 ----
            // 指数退避 1.5s→3s→6s→12s→24s，最多连续重启 8 次；恢复成功后计数归零。
            // 主动清理（kill_backend / RunEvent::Exit）时 BackendProcess 被 take 置 None，
            // watcher 检测到 None 即退出，不会误重启。
            {
                let h = app_handle.clone();
                let root = project_root.clone();
                let ddir = data_dir.clone();
                let rdir = resource_dir.clone();
                let lf = log_file.clone();
                std::thread::spawn(move || {
                    let mut restarts: u32 = 0;
                    loop {
                        std::thread::sleep(Duration::from_millis(800));
                        // 检测后端是否已退出（仅在 state 仍持有 child 时才算“意外退出”）
                        let exited = {
                            let Some(st) = h.try_state::<BackendProcess>() else { break };
                            let Ok(mut g) = st.0.lock() else { break };
                            match g.as_mut() {
                                None => break, // 主动清理，watcher 退出
                                Some(child) => matches!(child.try_wait(), Ok(Some(_))),
                            }
                        };
                        if !exited {
                            continue;
                        }
                        restarts += 1;
                        if restarts > 8 {
                            log_main(&h, &lf,
                                &format!("Backend crashed {} times consecutively, giving up auto-restart.", restarts),
                                "error");
                            break;
                        }
                        let delay_ms = 1500u64 * 2u64.pow(restarts - 1).min(16);
                        log_main(&h, &lf,
                            &format!("Backend exited, restarting in {}s (attempt {}/{})",
                                delay_ms / 1000, restarts, 8),
                            "warn");
                        std::thread::sleep(Duration::from_millis(delay_ms));

                        // 清掉已死的 child，再启动新进程
                        if let Some(st) = h.try_state::<BackendProcess>() {
                            if let Ok(mut g) = st.0.lock() {
                                g.take();
                            }
                        }
                        match start_backend(&root, &ddir, &rdir, port) {
                            Ok(mut new_child) => {
                                pipe_child_output(&mut new_child, h.clone(), lf.clone());
                                if let Some(st) = h.try_state::<BackendProcess>() {
                                    if let Ok(mut g) = st.0.lock() {
                                        *g = Some(new_child);
                                    }
                                }
                                let ok = tauri::async_runtime::block_on(async {
                                    tokio::time::timeout(
                                        Duration::from_secs(15),
                                        wait_for_backend(30, port),
                                    )
                                    .await
                                    .map_err(|_| "timeout".to_string())
                                    .and_then(|r| r)
                                });
                                if ok.is_ok() {
                                    log_main(&h, &lf, "Backend auto-restarted successfully.", "info");
                                    restarts = 0; // 恢复成功，重置计数
                                } else {
                                    log_main(&h, &lf,
                                        &format!("Backend restarted but not ready: {}", ok.unwrap_err()),
                                        "error");
                                }
                            }
                            Err(e) => {
                                log_main(&h, &lf,
                                    &format!("Backend auto-restart failed to spawn: {}", e),
                                    "error");
                            }
                        }
                    }
                });
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_app_info, log_message])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                log::info!("Window destroyed: {}", window.label());
                // 注意：这里【不】kill 后端。macOS 上关闭窗口默认不退出应用
                // （与 Electron 行为一致），若在此清理会导致重新打开窗口时
                // 后端已死、加载 http://127.0.0.1:<port> 失败。
                // 清理统一交给 RunEvent::Exit 兜底 + SIGTERM/SIGINT 信号兜底：
                //  - Windows/Linux：关窗触发 ExitRequested → Exit → 清理
                //  - macOS：Cmd+Q 退出时触发；仅关窗则后端保留，重开窗口正常
            }
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // 兜底清理：进程真正退出时确保 Python 后端被终止。
            // SIGTERM/SIGINT 信号兜底与本回调都调 kill_backend，幂等安全。
            if let tauri::RunEvent::Exit = event {
                log::info!("App exiting, cleaning up Python backend.");
                kill_backend(app_handle);
            }
        });
}

// ---------------------------------------------------------------------------
//  Unit tests
// ---------------------------------------------------------------------------
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_backend_origin_format() {
        assert_eq!(backend_origin(8787), "http://127.0.0.1:8787");
        assert_eq!(backend_origin(8791), "http://127.0.0.1:8791");
    }

    #[test]
    fn test_pick_backend_port_skips_busy() {
        // 占用一个端口，验证探测会跳过它
        let listener = std::net::TcpListener::bind(("127.0.0.1", 0)).unwrap();
        let busy_port = listener.local_addr().unwrap().port();
        assert!(!is_port_free(busy_port));
        // 探测应从 BACKEND_PORT 起找空闲端口，必然可用
        let picked = pick_backend_port();
        assert!(picked.is_some());
        assert!(is_port_free(picked.unwrap()));
        drop(listener);
    }
}