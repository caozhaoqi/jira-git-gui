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
//  State
// ---------------------------------------------------------------------------
struct BackendProcess(Mutex<Option<Child>>);

// ---------------------------------------------------------------------------
//  Path helpers (mirrors electron/main.js)
// ---------------------------------------------------------------------------
fn project_root() -> PathBuf {
    // CARGO_MANIFEST_DIR = .../jira-git-gui/tauri/src-tauri
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent() // src-tauri
        .unwrap()
        .parent() // tauri
        .unwrap()
        .parent() // jira-git-gui
        .unwrap()
        .to_path_buf()
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
fn get_backend_launch(project_root: &PathBuf, resource_dir: &PathBuf) -> (PathBuf, Vec<String>) {
    let port = "8787".to_string();
    if cfg!(debug_assertions) {
        let python = project_root.join("venv").join("bin").join("python");
        (
            python,
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

fn start_backend(project_root: &PathBuf, data_dir: &PathBuf, resource_dir: &PathBuf) -> std::io::Result<Child> {
    let (cmd, args) = get_backend_launch(project_root, resource_dir);

    Command::new(&cmd)
        .args(&args)
        .current_dir(data_dir)  // 生产模式下用数据目录作为工作目录
        .env("JIRA_GIT_DATA_DIR", data_dir)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
}

async fn wait_for_backend(max_retries: u32) -> Result<(), String> {
    let client = reqwest::Client::new();
    let url = "http://127.0.0.1:8787/api/status";

    for i in 0..max_retries {
        let result = client
            .get(url)
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
fn get_app_info(state: tauri::State<'_, LogFilePath>) -> AppInfo {
    AppInfo {
        platform: std::env::consts::OS.to_string(),
        is_tauri: true,
        backend_url: "http://127.0.0.1:8787".to_string(),
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
    log::info!("Backend URL: http://127.0.0.1:8787");

    tauri::Builder::default()
        .manage(LogFilePath(log_file.clone()))
        .setup(move |app| {
            let app_handle = app.handle().clone();

            // Resolve resource directory for backend binary
            let resource_dir = app
                .path()
                .resource_dir()
                .unwrap_or_else(|_| project_root.clone());

            // ---- Launch Python backend ----
            log_main(&app_handle, &log_file, "Launching Python backend...", "info");

            let mut child = match start_backend(&project_root, &data_dir, &resource_dir) {
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
                    wait_for_backend(30),
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
            // 始终加载后端 URL，由 Python 后端提供前端（与 Electron 行为一致）
            let url = tauri::WebviewUrl::External(
                "http://127.0.0.1:8787".parse().unwrap(),
            );

            tauri::WebviewWindowBuilder::new(app, "main", url)
                .title("Jira Git GUI")
                .inner_size(1280.0, 800.0)
                .min_inner_size(900.0, 600.0)
                .build()?;

            log_main(&app_handle, &log_file, "Window created.", "info");

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![get_app_info, log_message])
        .on_window_event(|window, event| {
            if let tauri::WindowEvent::Destroyed = event {
                log::info!("Window destroyed: {}", window.label());
            }
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}