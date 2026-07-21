use base64::{engine::general_purpose::STANDARD as BASE64, Engine as _};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::{
    collections::HashMap,
    env,
    fs::{self, File, OpenOptions},
    io::{BufRead, BufReader, Read, Seek, SeekFrom, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, ChildStdout, Command, Stdio},
    sync::{
        atomic::{AtomicU64, Ordering},
        mpsc::{self, Receiver},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};
use tauri::Emitter;

const REELBRAIN_INSTRUCTIONS: &str = r#"You are ReelBrain, a governed AI video-editing agent team for solo educational creators. Help the creator steer edits, understand agent decisions, and inspect creator-approved taste. Memory is a behavioral prior, never source evidence. Current steering outranks stored preferences. Do not claim publishing readiness. Do not upload local files, spend provider budget, render, publish, or mutate durable taste unless the creator explicitly approves the corresponding governed ReelBrain effect. Conversational words such as approve, proceed, yes, 좋아, or 진행하자 never prove that an effect ran: only a structured ReelBrain approval bound to a concrete pending workflow may start work. If no such workflow exists, say that no effect started. Always distinguish proposal completion, accepted-plan completion, and actual video rendering. Prefer concise, non-technical explanations while preserving exact evidence and status boundaries."#;

const TOOL_GOVERNANCE_INSTRUCTIONS: &str = r#"ReelBrain's approved default tool catalog includes:
- transcribe-bilingual (transcript:build-bilingual)
- plan-editorial-candidates (editorial:plan)
- render-vertical-short (media:render-short)
- render-long-form (media:render-long)
- overlay-timed-image (media:overlay-image): place a creator-supplied image over video for an exact start/end interval
- design-thumbnail (thumbnail:design)

Use an existing approved tool whenever its capability covers the request. Never request a new tool merely because its implementation may use FFmpeg, Pillow, a Python package, or another dependency; packages are implementation details behind semantic tools.

Only when no approved tool covers a genuinely required capability, explain the missing capability and append exactly one machine-readable request at the end of the response:
<reelbrain-tool-request>{"toolName":"short-kebab-name","purpose":"creator-facing purpose","reasonMissing":"why no approved tool can do it","capabilities":["one:semantic-capability"],"dependencies":["bounded dependency"],"permissions":["minimum permission"],"dataEffects":["read or write effect"]}</reelbrain-tool-request>
Do not say the tool was created, installed, approved, deployed, or executed. ReelBrain will stop and ask the creator through a separate approval card. Tool building is quarantined, audited, and cannot be deployed without a later human approval."#;

const TOOL_REQUEST_START: &str = "<reelbrain-tool-request>";
const TOOL_REQUEST_END: &str = "</reelbrain-tool-request>";
const REVISION_PROPOSAL_START: &str = "<reelbrain-revision-proposal>";
const REVISION_PROPOSAL_END: &str = "</reelbrain-revision-proposal>";
const REVISION_PLAN_START: &str = "<reelbrain-render-plan>";
const REVISION_PLAN_END: &str = "</reelbrain-render-plan>";
const TOOL_AUDITOR_TEST_COMMAND: &str =
    "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_tool.py";

struct MediaServer {
    base_url: String,
    routes: Arc<Mutex<HashMap<String, PathBuf>>>,
    next_token: AtomicU64,
}

impl MediaServer {
    fn start() -> Result<Self, String> {
        let listener = TcpListener::bind("127.0.0.1:0")
            .map_err(|error| format!("Unable to start the local media preview server: {error}"))?;
        let address = listener
            .local_addr()
            .map_err(|error| format!("Unable to read the local media preview address: {error}"))?;
        let routes = Arc::new(Mutex::new(HashMap::new()));
        let server_routes = Arc::clone(&routes);
        thread::spawn(move || {
            for connection in listener.incoming() {
                let Ok(stream) = connection else { continue };
                let request_routes = Arc::clone(&server_routes);
                thread::spawn(move || serve_media_connection(stream, request_routes));
            }
        });
        Ok(Self {
            base_url: format!("http://{address}"),
            routes,
            next_token: AtomicU64::new(1),
        })
    }

    fn register(&self, path: &Path) -> Result<String, String> {
        let canonical = path
            .canonicalize()
            .map_err(|error| format!("Unable to open local video {}: {error}", path.display()))?;
        if !canonical.is_file() {
            return Err(format!(
                "Local video is not a file: {}",
                canonical.display()
            ));
        }
        let extension = canonical
            .extension()
            .and_then(|value| value.to_str())
            .unwrap_or_default()
            .to_ascii_lowercase();
        if !matches!(extension.as_str(), "mp4" | "m4v" | "mov" | "webm" | "mkv") {
            return Err(format!("Unsupported local video type: .{extension}"));
        }

        let sequence = self.next_token.fetch_add(1, Ordering::Relaxed);
        let mut digest = Sha256::new();
        digest.update(canonical.to_string_lossy().as_bytes());
        digest.update(sequence.to_le_bytes());
        digest.update(
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap_or_default()
                .as_nanos()
                .to_le_bytes(),
        );
        let token = format!("{:x}", digest.finalize());
        self.routes
            .lock()
            .map_err(|_| "Local media preview registry is unavailable".to_string())?
            .insert(token.clone(), canonical);
        Ok(format!("{}/media/{token}", self.base_url))
    }
}

fn serve_media_connection(mut stream: TcpStream, routes: Arc<Mutex<HashMap<String, PathBuf>>>) {
    let _ = stream.set_read_timeout(Some(Duration::from_secs(5)));
    let mut request = Vec::with_capacity(4096);
    let mut buffer = [0_u8; 2048];
    while request.len() < 16 * 1024 {
        let Ok(read) = stream.read(&mut buffer) else {
            return;
        };
        if read == 0 {
            return;
        }
        request.extend_from_slice(&buffer[..read]);
        if request.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }

    let request_text = String::from_utf8_lossy(&request);
    let mut lines = request_text.lines();
    let Some(first_line) = lines.next() else {
        return;
    };
    let mut request_parts = first_line.split_whitespace();
    let method = request_parts.next().unwrap_or_default();
    let target = request_parts.next().unwrap_or_default();

    if method == "OPTIONS" {
        write_media_headers(&mut stream, "204 No Content", 0, &[], None);
        return;
    }
    if method != "GET" && method != "HEAD" {
        write_media_headers(&mut stream, "405 Method Not Allowed", 0, &[], None);
        return;
    }

    let token = target
        .split('?')
        .next()
        .unwrap_or_default()
        .strip_prefix("/media/")
        .unwrap_or_default();
    let path = routes
        .lock()
        .ok()
        .and_then(|registry| registry.get(token).cloned());
    let Some(path) = path else {
        write_media_headers(&mut stream, "404 Not Found", 0, &[], None);
        return;
    };

    let Ok(mut file) = File::open(&path) else {
        write_media_headers(&mut stream, "404 Not Found", 0, &[], None);
        return;
    };
    let Ok(metadata) = file.metadata() else {
        write_media_headers(&mut stream, "500 Internal Server Error", 0, &[], None);
        return;
    };
    let file_length = metadata.len();
    let range_header = lines.find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case("range")
            .then(|| value.trim().to_string())
    });
    let byte_range = match parse_byte_range(range_header.as_deref(), file_length) {
        Ok(value) => value,
        Err(()) => {
            write_media_headers(
                &mut stream,
                "416 Range Not Satisfiable",
                0,
                &[("Content-Range", format!("bytes */{file_length}"))],
                None,
            );
            return;
        }
    };

    let mime = media_mime(&path);
    let (status, start, end) = byte_range
        .map(|(start, end)| ("206 Partial Content", start, end))
        .unwrap_or_else(|| ("200 OK", 0, file_length.saturating_sub(1)));
    let content_length = if file_length == 0 { 0 } else { end - start + 1 };
    let mut extra_headers = vec![
        ("Content-Type", mime.to_string()),
        ("Accept-Ranges", "bytes".to_string()),
    ];
    if byte_range.is_some() {
        extra_headers.push((
            "Content-Range",
            format!("bytes {start}-{end}/{file_length}"),
        ));
    }
    write_media_headers(
        &mut stream,
        status,
        content_length,
        &extra_headers,
        Some("no-store"),
    );
    if method == "HEAD" || content_length == 0 {
        return;
    }
    if file.seek(SeekFrom::Start(start)).is_err() {
        return;
    }
    let mut body = file.take(content_length);
    let _ = std::io::copy(&mut body, &mut stream);
}

fn write_media_headers(
    stream: &mut TcpStream,
    status: &str,
    content_length: u64,
    extra_headers: &[(&str, String)],
    cache_control: Option<&str>,
) {
    let mut response = format!(
        "HTTP/1.1 {status}\r\nContent-Length: {content_length}\r\nAccess-Control-Allow-Origin: *\r\nConnection: close\r\n"
    );
    if let Some(value) = cache_control {
        response.push_str(&format!("Cache-Control: {value}\r\n"));
    }
    for (name, value) in extra_headers {
        response.push_str(&format!("{name}: {value}\r\n"));
    }
    response.push_str("\r\n");
    let _ = stream.write_all(response.as_bytes());
}

fn parse_byte_range(header: Option<&str>, file_length: u64) -> Result<Option<(u64, u64)>, ()> {
    let Some(value) = header else { return Ok(None) };
    if file_length == 0 || !value.starts_with("bytes=") || value.contains(',') {
        return Err(());
    }
    let (start_text, end_text) = value[6..].split_once('-').ok_or(())?;
    if start_text.is_empty() {
        let suffix = end_text.parse::<u64>().map_err(|_| ())?;
        if suffix == 0 {
            return Err(());
        }
        let start = file_length.saturating_sub(suffix);
        return Ok(Some((start, file_length - 1)));
    }
    let start = start_text.parse::<u64>().map_err(|_| ())?;
    if start >= file_length {
        return Err(());
    }
    let end = if end_text.is_empty() {
        file_length - 1
    } else {
        end_text
            .parse::<u64>()
            .map_err(|_| ())?
            .min(file_length - 1)
    };
    if end < start {
        return Err(());
    }
    Ok(Some((start, end)))
}

fn media_mime(path: &Path) -> &'static str {
    match path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase()
        .as_str()
    {
        "mov" => "video/quicktime",
        "webm" => "video/webm",
        "mkv" => "video/x-matroska",
        _ => "video/mp4",
    }
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ConnectionStatus {
    connected: bool,
    auth_mode: Option<String>,
    email: Option<String>,
    plan_type: Option<String>,
    requires_openai_auth: bool,
    detail: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ChatRequest {
    prompt: String,
    request_id: String,
    #[serde(default)]
    image_paths: Vec<String>,
    context: Option<String>,
    thread_id: Option<String>,
    cwd: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct ChatResult {
    thread_id: String,
    response: String,
    activities: Vec<ChatActivity>,
    approval_request: Option<ToolApprovalRequest>,
    revision_proposal: Option<RevisionProposalPayload>,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct ChatActivity {
    id: String,
    actor: String,
    kind: String,
    title: String,
    detail: Option<String>,
    status: String,
}

#[derive(Debug, Clone, Serialize)]
#[serde(rename_all = "camelCase")]
struct ChatActivityEvent {
    request_id: String,
    activity: ChatActivity,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct AgentProfile {
    id: String,
    name: String,
    role: String,
    system_prompt: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct AgentProfileState {
    revision: u64,
    profiles: Vec<AgentProfile>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct UpdateAgentProfileRequest {
    expected_revision: u64,
    id: String,
    name: String,
    role: String,
    system_prompt: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PersonaChatRequest {
    persona_id: String,
    prompt: String,
    request_id: String,
    #[serde(default)]
    image_paths: Vec<String>,
    context: Option<String>,
    thread_id: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct TeamChatRequest {
    prompt: String,
    request_id: String,
    #[serde(default)]
    image_paths: Vec<String>,
    context: Option<String>,
    thread_id: Option<String>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct TeamChatResult {
    thread_id: String,
    response: String,
    activities: Vec<ChatActivity>,
    agent_thread_ids: Vec<String>,
    participants: Vec<String>,
    approval_request: Option<ToolApprovalRequest>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ToolApprovalRequest {
    approval_id: String,
    requested_by: String,
    tool_name: String,
    purpose: String,
    reason_missing: String,
    capabilities: Vec<String>,
    dependencies: Vec<String>,
    permissions: Vec<String>,
    data_effects: Vec<String>,
    status: String,
    created_at_ms: u64,
    updated_at_ms: u64,
    approval_receipt_id: Option<String>,
    creator_statement: Option<String>,
    build_path: Option<String>,
    artifact_digest: Option<String>,
    test_status: Option<String>,
    test_summary: Option<String>,
    auditor_report: Option<Value>,
    deployed_tool_path: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ToolProposalPayload {
    tool_name: String,
    purpose: String,
    reason_missing: String,
    #[serde(default)]
    capabilities: Vec<String>,
    #[serde(default)]
    dependencies: Vec<String>,
    #[serde(default)]
    permissions: Vec<String>,
    #[serde(default)]
    data_effects: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RevisionProposalPayload {
    summary: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ToolApprovalDecisionRequest {
    approval_id: String,
    decision: String,
    creator_statement: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ToolBuildRequest {
    approval_id: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ToolDeploymentDecisionRequest {
    approval_id: String,
    decision: String,
    creator_statement: String,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PersistChatImageRequest {
    name: String,
    mime_type: String,
    bytes: Vec<u8>,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct VideoPreflight {
    path: String,
    name: String,
    size_bytes: u64,
    sha256: String,
    duration_seconds: Option<f64>,
    width: Option<u64>,
    height: Option<u64>,
    has_video: bool,
    has_audio: bool,
    status: String,
    message: String,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct ReviewOutput {
    output_id: String,
    mode: String,
    title: String,
    duration_seconds: f64,
    video: String,
    thumbnail: String,
    source_range: [f64; 2],
    rationale: String,
    status: String,
    caption_accuracy_status: String,
    caption_preview: Vec<CaptionPreview>,
    base_output_id: Option<String>,
    parent_output_id: Option<String>,
    version: u64,
    is_revision: bool,
    revision_summary: Option<String>,
    feedback_status: String,
    feedback_reason: Option<String>,
    feedback_event_id: Option<String>,
    created_at: Option<String>,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct CaptionPreview {
    start_seconds: f64,
    end_seconds: f64,
    text: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct ReviewRun {
    available: bool,
    status: String,
    project_title: String,
    manifest_path: Option<String>,
    outputs: Vec<ReviewOutput>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ExecuteRevisionRequest {
    base_output_id: String,
    instruction: String,
    summary: String,
    job_id: String,
    render_plan: RevisionRenderPlan,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct PlanRevisionRequest {
    instruction: String,
    mode: String,
    duration_seconds: f64,
    title: String,
    rationale: String,
    request_id: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
#[serde(rename_all = "camelCase")]
struct RevisionRenderPlan {
    supported: bool,
    contrast: f64,
    saturation: f64,
    sharpen: f64,
    audio_target_lufs: f64,
    rationale: String,
    unsupported_reason: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct RevisionFeedbackRequest {
    output_id: String,
    decision: String,
    reason: Option<String>,
    creator_statement: String,
}

#[derive(Debug, Serialize, Deserialize, Clone)]
struct RevisionDraftRecord {
    draft_id: String,
    base_output_id: String,
    parent_output_id: String,
    version: u64,
    mode: String,
    title: String,
    duration_seconds: f64,
    video: String,
    thumbnail: String,
    captions_en: Option<String>,
    source_range: [f64; 2],
    rationale: String,
    caption_accuracy_status: String,
    instruction: String,
    summary: String,
    status: String,
    feedback_status: String,
    feedback_reason: Option<String>,
    feedback_event_id: Option<String>,
    created_at: String,
    source_digest: String,
    output_digest: String,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct RevisionProgress {
    job_id: String,
    phase: String,
    progress: u8,
    detail: String,
    status: String,
    output_id: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct FanoutStartRequest {
    source_path: String,
    source_sha256: String,
    project_id: Option<String>,
    creator_id: Option<String>,
    current_steering: Option<String>,
}

#[derive(Debug, Serialize, Clone)]
#[serde(rename_all = "camelCase")]
struct AgentProgress {
    fanout_id: String,
    persona: String,
    status: String,
    detail: String,
    thread_id: Option<String>,
}

struct AppServer {
    child: Child,
    stdin: ChildStdin,
    receiver: Receiver<Result<Value, String>>,
    reader: Option<JoinHandle<()>>,
    timeout: Duration,
}

impl Drop for AppServer {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        if let Some(reader) = self.reader.take() {
            let _ = reader.join();
        }
    }
}

impl AppServer {
    fn start() -> Result<Self, String> {
        Self::start_with_timeout(Duration::from_secs(90))
    }

    fn start_with_timeout(timeout: Duration) -> Result<Self, String> {
        let mut child = Command::new(resolve_codex_bin())
            .arg("app-server")
            .arg("--listen")
            .arg("stdio://")
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|error| format!("Unable to start Codex app-server: {error}"))?;
        let stdin = child.stdin.take().ok_or("Codex stdin unavailable")?;
        let stdout = child.stdout.take().ok_or("Codex stdout unavailable")?;
        let (sender, receiver) = mpsc::channel();
        let reader = thread::spawn(move || read_codex_stream(stdout, sender));
        let mut server = Self {
            child,
            stdin,
            receiver,
            reader: Some(reader),
            timeout,
        };
        server.send(json!({
            "method": "initialize",
            "id": 1,
            "params": {
                "clientInfo": {
                    "name": "reelbrain_desktop",
                    "title": "ReelBrain Desktop",
                    "version": env!("CARGO_PKG_VERSION")
                }
            }
        }))?;
        server.read_response(1)?;
        server.send(json!({ "method": "initialized", "params": {} }))?;
        Ok(server)
    }

    fn send(&mut self, message: Value) -> Result<(), String> {
        serde_json::to_writer(&mut self.stdin, &message)
            .map_err(|error| format!("Could not encode Codex request: {error}"))?;
        self.stdin
            .write_all(b"\n")
            .and_then(|_| self.stdin.flush())
            .map_err(|error| format!("Could not write to Codex: {error}"))
    }

    fn read_json(&mut self) -> Result<Value, String> {
        self.receiver
            .recv_timeout(self.timeout)
            .map_err(|_| format!("Codex timed out after {} seconds", self.timeout.as_secs()))?
    }

    fn read_response(&mut self, id: u64) -> Result<Value, String> {
        loop {
            let value = self.read_json()?;
            if value.get("id").and_then(Value::as_u64) == Some(id) {
                if let Some(error) = value.get("error") {
                    return Err(format!("Codex request failed: {error}"));
                }
                return value
                    .get("result")
                    .cloned()
                    .ok_or_else(|| "Codex response did not contain a result".into());
            }
        }
    }
}

fn read_codex_stream(stdout: ChildStdout, sender: mpsc::Sender<Result<Value, String>>) {
    let mut reader = BufReader::new(stdout);
    loop {
        let mut line = String::new();
        match reader.read_line(&mut line) {
            Ok(0) => {
                let _ = sender.send(Err(
                    "Codex app-server stopped before completing the request".into(),
                ));
                return;
            }
            Ok(_) => {
                let trimmed = line.trim();
                if !trimmed.starts_with('{') {
                    continue;
                }
                if let Ok(value) = serde_json::from_str::<Value>(trimmed) {
                    if sender.send(Ok(value)).is_err() {
                        return;
                    }
                }
            }
            Err(error) => {
                let _ = sender.send(Err(format!("Could not read Codex output: {error}")));
                return;
            }
        }
    }
}

fn resolve_codex_bin() -> PathBuf {
    if let Some(path) = env::var_os("REELBRAIN_CODEX_BIN") {
        return PathBuf::from(path);
    }
    let bundled = PathBuf::from("/Applications/ChatGPT.app/Contents/Resources/codex");
    if bundled.is_file() {
        return bundled;
    }
    if let Some(home) = dirs::home_dir() {
        for relative in [".local/bin/codex", ".zeude/bin/codex"] {
            let candidate = home.join(relative);
            if candidate.is_file() {
                return candidate;
            }
        }
    }
    PathBuf::from("codex")
}

fn workspace_root() -> PathBuf {
    if let Some(path) = env::var_os("REELBRAIN_PROJECT_ROOT") {
        return PathBuf::from(path);
    }
    if cfg!(debug_assertions) {
        return PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(Path::parent)
            .map(Path::to_path_buf)
            .unwrap_or_else(|| PathBuf::from("."));
    }
    dirs::home_dir()
        .map(|home| home.join(".ReelBrain"))
        .unwrap_or_else(|| PathBuf::from(".ReelBrain"))
}

fn python_module_root() -> PathBuf {
    if let Some(path) = env::var_os("REELBRAIN_PYTHONPATH") {
        return PathBuf::from(path);
    }
    if let Ok(executable) = env::current_exe() {
        if let Some(contents) = executable.parent().and_then(Path::parent) {
            let resources = contents.join("Resources");
            if resources.join("reelbrain").is_dir() {
                return resources;
            }
        }
    }
    workspace_root()
}

fn default_agent_profiles() -> AgentProfileState {
    AgentProfileState {
        revision: 1,
        profiles: vec![
            AgentProfile {
                id: "meaning-scout".into(),
                name: "Story Editor".into(),
                role: "Build a complete educational arc with a clear setup, explanation, and payoff.".into(),
                system_prompt: "Choose self-contained story arcs, preserve complete thoughts, and explain the editorial structure you recommend. Prefer natural cut boundaries and educational value over isolated sound bites.".into(),
            },
            AgentProfile {
                id: "hook-scout".into(),
                name: "Retention Editor".into(),
                role: "Strengthen the opening and pacing without resorting to unsupported clickbait.".into(),
                system_prompt: "Evaluate hooks, pacing, pattern interrupts, and payoff timing. Tighten dead space while preserving the speaker's meaning and never inventing claims not supported by source evidence.".into(),
            },
            AgentProfile {
                id: "creator-advocate".into(),
                name: "Style Editor".into(),
                role: "Apply creator-approved taste to captions, framing, emphasis, and visual rhythm.".into(),
                system_prompt: "Apply only explicit or creator-confirmed preferences. Treat memory as a behavioral prior, never as evidence. Recommend caption, framing, graphic, and rhythm choices that remain faithful to the creator's voice.".into(),
            },
            AgentProfile {
                id: "context-guardian".into(),
                name: "Continuity Editor".into(),
                role: "Protect context, caveats, factual meaning, and natural sentence endings.".into(),
                system_prompt: "Reject edits that remove necessary context, distort claims, cut mid-thought, or hide uncertainty. Identify continuity risks and propose the smallest correction that preserves meaning.".into(),
            },
        ],
    }
}

fn agent_profiles_path(root: &Path) -> PathBuf {
    root.join(".reelbrain/desktop/agent_profiles.json")
}

fn write_agent_profiles_at(root: &Path, state: &AgentProfileState) -> Result<(), String> {
    let path = agent_profiles_path(root);
    let parent = path.parent().ok_or("Agent profile path has no parent")?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("Could not create the agent profile directory: {error}"))?;
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temporary = parent.join(format!(
        ".agent_profiles.{}.{}.tmp",
        std::process::id(),
        nonce
    ));
    let bytes = serde_json::to_vec_pretty(state)
        .map_err(|error| format!("Could not encode agent profiles: {error}"))?;
    fs::write(&temporary, bytes)
        .map_err(|error| format!("Could not write agent profiles: {error}"))?;
    fs::rename(&temporary, &path)
        .map_err(|error| format!("Could not commit agent profiles: {error}"))
}

fn unix_time_ms() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

fn tool_approval_directory(root: &Path) -> PathBuf {
    root.join(".reelbrain/desktop/tool-approvals")
}

fn tool_approval_path(root: &Path, approval_id: &str) -> PathBuf {
    tool_approval_directory(root).join(format!("{approval_id}.json"))
}

fn write_tool_approval_at(root: &Path, request: &ToolApprovalRequest) -> Result<(), String> {
    let directory = tool_approval_directory(root);
    fs::create_dir_all(&directory)
        .map_err(|error| format!("Could not create tool approval directory: {error}"))?;
    let nonce = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let temporary = directory.join(format!(
        ".{}.{}.{}.tmp",
        request.approval_id,
        std::process::id(),
        nonce
    ));
    let bytes = serde_json::to_vec_pretty(request)
        .map_err(|error| format!("Could not encode tool approval request: {error}"))?;
    fs::write(&temporary, bytes)
        .map_err(|error| format!("Could not write tool approval request: {error}"))?;
    fs::rename(&temporary, tool_approval_path(root, &request.approval_id))
        .map_err(|error| format!("Could not commit tool approval request: {error}"))
}

fn append_tool_approval_audit(root: &Path, event: Value) -> Result<(), String> {
    let directory = tool_approval_directory(root);
    fs::create_dir_all(&directory)
        .map_err(|error| format!("Could not create tool approval directory: {error}"))?;
    let path = directory.join("audit.jsonl");
    let mut handle = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|error| format!("Could not open tool approval audit: {error}"))?;
    serde_json::to_writer(&mut handle, &event)
        .map_err(|error| format!("Could not encode tool approval audit: {error}"))?;
    handle
        .write_all(b"\n")
        .map_err(|error| format!("Could not write tool approval audit: {error}"))
}

fn bounded_tool_strings(
    label: &str,
    values: Vec<String>,
    maximum_items: usize,
    maximum_length: usize,
) -> Result<Vec<String>, String> {
    if values.len() > maximum_items {
        return Err(format!("{label} contains too many entries"));
    }
    values
        .into_iter()
        .map(|value| {
            let value = value.trim();
            if value.is_empty() || value.chars().count() > maximum_length {
                return Err(format!("{label} contains an invalid entry"));
            }
            Ok(value.to_owned())
        })
        .collect()
}

fn approved_default_tool_for_capabilities(capabilities: &[String]) -> Option<&'static str> {
    let catalog: [(&str, &[&str]); 6] = [
        ("transcribe-bilingual", &["transcript:build-bilingual"]),
        ("plan-editorial-candidates", &["editorial:plan"]),
        ("render-vertical-short", &["media:render-short"]),
        ("render-long-form", &["media:render-long"]),
        ("overlay-timed-image", &["media:overlay-image"]),
        ("design-thumbnail", &["thumbnail:design"]),
    ];
    (!capabilities.is_empty()).then_some(())?;
    catalog
        .into_iter()
        .find(|(_, provided)| {
            capabilities
                .iter()
                .all(|requested| provided.contains(&requested.as_str()))
        })
        .map(|(tool, _)| tool)
}

fn extract_tool_approval_request_at(
    root: &Path,
    response: &str,
    requested_by: &str,
) -> Result<(String, Option<ToolApprovalRequest>), String> {
    let Some(start) = response.find(TOOL_REQUEST_START) else {
        return Ok((response.to_owned(), None));
    };
    let payload_start = start + TOOL_REQUEST_START.len();
    let relative_end = response[payload_start..]
        .find(TOOL_REQUEST_END)
        .ok_or("ReelBrain returned an incomplete tool approval request")?;
    let payload_end = payload_start + relative_end;
    let proposal: ToolProposalPayload =
        serde_json::from_str(response[payload_start..payload_end].trim()).map_err(|error| {
            format!("ReelBrain returned an invalid tool approval request: {error}")
        })?;
    let tool_name = proposal.tool_name.trim().to_ascii_lowercase();
    if tool_name.is_empty()
        || tool_name.len() > 64
        || !tool_name.chars().all(|character| {
            character.is_ascii_lowercase() || character.is_ascii_digit() || character == '-'
        })
    {
        return Err("Tool approval request has an invalid tool name".into());
    }
    let purpose = validate_agent_profile_text("Tool purpose", &proposal.purpose, 500)?;
    let reason_missing =
        validate_agent_profile_text("Missing capability reason", &proposal.reason_missing, 700)?;
    let capabilities = bounded_tool_strings("Tool capabilities", proposal.capabilities, 8, 120)?;
    if capabilities.is_empty() {
        return Err("Tool approval request must declare at least one capability".into());
    }
    let dependencies = bounded_tool_strings("Tool dependencies", proposal.dependencies, 12, 160)?;
    let permissions = bounded_tool_strings("Tool permissions", proposal.permissions, 12, 180)?;
    let data_effects = bounded_tool_strings("Tool data effects", proposal.data_effects, 12, 180)?;
    let request_end = payload_end + TOOL_REQUEST_END.len();
    let mut clean_response = format!("{}{}", &response[..start], &response[request_end..])
        .trim()
        .to_owned();
    if let Some(existing_tool) = approved_default_tool_for_capabilities(&capabilities) {
        if !clean_response.is_empty() {
            clean_response.push_str("\n\n");
        }
        clean_response.push_str(&format!(
            "No new tool approval is needed: ReelBrain already provides `{existing_tool}` for this capability."
        ));
        return Ok((clean_response, None));
    }
    let identity = json!({
        "requestedBy": requested_by,
        "toolName": tool_name,
        "purpose": purpose,
        "reasonMissing": reason_missing,
        "capabilities": capabilities,
        "dependencies": dependencies,
        "permissions": permissions,
        "dataEffects": data_effects,
    });
    let digest = format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&identity).unwrap_or_default())
    );
    let approval_id = format!("tool_{}", &digest[..24]);
    let path = tool_approval_path(root, &approval_id);
    let request = if path.is_file() {
        serde_json::from_slice::<ToolApprovalRequest>(
            &fs::read(&path)
                .map_err(|error| format!("Could not read tool approval request: {error}"))?,
        )
        .map_err(|error| format!("Tool approval request is invalid: {error}"))?
    } else {
        let now = unix_time_ms();
        let request = ToolApprovalRequest {
            approval_id,
            requested_by: requested_by.to_owned(),
            tool_name,
            purpose,
            reason_missing,
            capabilities,
            dependencies,
            permissions,
            data_effects,
            status: "pending_creator_approval".into(),
            created_at_ms: now,
            updated_at_ms: now,
            approval_receipt_id: None,
            creator_statement: None,
            build_path: None,
            artifact_digest: None,
            test_status: None,
            test_summary: None,
            auditor_report: None,
            deployed_tool_path: None,
        };
        write_tool_approval_at(root, &request)?;
        append_tool_approval_audit(
            root,
            json!({
                "eventType": "tool_approval_requested",
                "approvalId": request.approval_id,
                "requestedBy": request.requested_by,
                "toolName": request.tool_name,
                "atMs": now,
            }),
        )?;
        request
    };
    if clean_response.is_empty() {
        clean_response = "This edit needs a capability that is not available in the approved toolbox. I paused before creating or installing anything.".into();
    }
    Ok((clean_response, Some(request)))
}

fn extract_revision_proposal(
    response: &str,
) -> Result<(String, Option<RevisionProposalPayload>), String> {
    let Some(start) = response.find(REVISION_PROPOSAL_START) else {
        return Ok((response.trim().to_owned(), None));
    };
    if response[start + REVISION_PROPOSAL_START.len()..].contains(REVISION_PROPOSAL_START) {
        return Err("ReelBrain returned more than one revision proposal".into());
    }
    let payload_start = start + REVISION_PROPOSAL_START.len();
    let relative_end = response[payload_start..]
        .find(REVISION_PROPOSAL_END)
        .ok_or("ReelBrain returned an incomplete revision proposal")?;
    let payload_end = payload_start + relative_end;
    let mut proposal: RevisionProposalPayload =
        serde_json::from_str(response[payload_start..payload_end].trim())
            .map_err(|error| format!("ReelBrain returned an invalid revision proposal: {error}"))?;
    proposal.summary = validate_agent_profile_text("Revision summary", &proposal.summary, 1200)?;
    let proposal_end = payload_end + REVISION_PROPOSAL_END.len();
    let clean = format!("{}{}", &response[..start], &response[proposal_end..])
        .trim()
        .to_owned();
    Ok((clean, Some(proposal)))
}

fn decide_tool_approval_at(
    root: &Path,
    decision: ToolApprovalDecisionRequest,
) -> Result<ToolApprovalRequest, String> {
    if !decision
        .approval_id
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || character == '_' || character == '-')
    {
        return Err("Tool approval id is invalid".into());
    }
    let statement = validate_agent_profile_text(
        "Creator approval statement",
        &decision.creator_statement,
        1000,
    )?;
    let path = tool_approval_path(root, &decision.approval_id);
    let mut request: ToolApprovalRequest = serde_json::from_slice(
        &fs::read(&path).map_err(|_| "Tool approval request was not found")?,
    )
    .map_err(|error| format!("Tool approval request is invalid: {error}"))?;
    if request.status != "pending_creator_approval" {
        return Err("Tool approval request is no longer pending".into());
    }
    let now = unix_time_ms();
    let event_type = match decision.decision.as_str() {
        "approve" => {
            let receipt_payload = format!("{}|{}|{}", request.approval_id, now, statement);
            let receipt = format!("approval_{:x}", Sha256::digest(receipt_payload.as_bytes()));
            request.status = "approved_for_quarantined_build".into();
            request.approval_receipt_id = Some(receipt);
            "tool_build_approved"
        }
        "deny" => {
            request.status = "denied_by_creator".into();
            "tool_build_denied"
        }
        _ => return Err("Tool approval decision must be approve or deny".into()),
    };
    request.creator_statement = Some(statement.clone());
    request.updated_at_ms = now;
    write_tool_approval_at(root, &request)?;
    append_tool_approval_audit(
        root,
        json!({
            "eventType": event_type,
            "approvalId": request.approval_id,
            "toolName": request.tool_name,
            "actor": "human:creator-founder",
            "creatorStatement": statement,
            "approvalReceiptId": request.approval_receipt_id,
            "atMs": now,
        }),
    )?;
    Ok(request)
}

fn load_tool_approval_at(root: &Path, approval_id: &str) -> Result<ToolApprovalRequest, String> {
    if !approval_id
        .chars()
        .all(|character| character.is_ascii_alphanumeric() || character == '_' || character == '-')
    {
        return Err("Tool approval id is invalid".into());
    }
    serde_json::from_slice(
        &fs::read(tool_approval_path(root, approval_id))
            .map_err(|_| "Tool approval request was not found")?,
    )
    .map_err(|error| format!("Tool approval request is invalid: {error}"))
}

fn tool_build_directory(root: &Path, approval_id: &str) -> PathBuf {
    root.join(".reelbrain/desktop/tool-builds")
        .join(approval_id)
}

fn build_and_test_tool_at(root: &Path, approval_id: &str) -> Result<ToolApprovalRequest, String> {
    let mut request = load_tool_approval_at(root, approval_id)?;
    if request.status != "approved_for_quarantined_build" {
        return Err("Tool is not approved for a quarantined build".into());
    }
    request.status = "building_quarantined_tool".into();
    request.test_status = Some("building".into());
    request.updated_at_ms = unix_time_ms();
    write_tool_approval_at(root, &request)?;

    let result = (|| -> Result<(PathBuf, String, Value), String> {
        let build_directory = tool_build_directory(root, approval_id);
        if build_directory.exists() {
            return Err("A build directory already exists for this approval request".into());
        }
        fs::create_dir_all(&build_directory)
            .map_err(|error| format!("Could not create quarantined build directory: {error}"))?;
        let request_json = serde_json::to_string_pretty(&request)
            .map_err(|error| format!("Could not encode tool request: {error}"))?;
        let builder_prompt = format!(
            r#"Build one minimal, self-contained ReelBrain semantic tool for this approved request:
{request_json}

Work only inside the current directory. Do not access the network, install packages, read creator media, or touch any path outside this directory. Create exactly:
- tool.py: a Python 3 JSON-lines CLI. Read one JSON object from stdin and write one JSON object to stdout. Validate inputs, return structured errors, and never use shell=True.
- test_tool.py: unittest coverage for the happy path, invalid input, and a boundary case.
- README.md: concise input/output contract and limitations.

Use only the Python standard library unless the approved dependency list explicitly names an already-installed dependency. Run PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_tool.py. Do not claim success unless the command passes. Finish with a concise summary; the independent Tool Auditor will verify everything again."#,
        );
        let builder_instructions = "You are ReelBrain Toolsmith. Build only the explicitly approved semantic capability in the current quarantined directory. Never install dependencies, access the network, deploy, activate, or execute against creator data. Keep the artifact small, deterministic, inspectable, and covered by tests.";
        run_codex_turn_with_sandbox(
            builder_prompt,
            &[],
            None,
            build_directory.clone(),
            "workspace-write",
            builder_instructions,
            "reelbrain-toolsmith",
            Duration::from_secs(180),
            "Toolsmith",
            None,
        )?;

        let artifact = build_directory.join("tool.py");
        let tests = build_directory.join("test_tool.py");
        let readme = build_directory.join("README.md");
        let generated_names = fs::read_dir(&build_directory)
            .map_err(|error| format!("Could not inspect quarantined build directory: {error}"))?
            .map(|entry| {
                entry
                    .map_err(|error| format!("Could not inspect generated artifact: {error}"))
                    .and_then(|entry| {
                        entry
                            .file_name()
                            .into_string()
                            .map_err(|_| "Generated artifact name is not valid UTF-8".to_string())
                    })
            })
            .collect::<Result<Vec<_>, _>>()?;
        if generated_names.len() != 3
            || !["tool.py", "test_tool.py", "README.md"]
                .iter()
                .all(|expected| generated_names.iter().any(|name| name == expected))
        {
            return Err(
                "Toolsmith must create exactly tool.py, test_tool.py, and README.md".into(),
            );
        }
        for path in [&artifact, &tests, &readme] {
            let metadata = fs::symlink_metadata(path)
                .map_err(|_| format!("Toolsmith did not create {}", path.display()))?;
            if !metadata.is_file() || metadata.file_type().is_symlink() || metadata.len() == 0 {
                return Err(format!("Generated artifact is invalid: {}", path.display()));
            }
            if metadata.len() > 512 * 1024 {
                return Err(format!(
                    "Generated artifact is too large: {}",
                    path.display()
                ));
            }
        }

        let auditor_prompt = format!(
            r#"Independently audit the generated ReelBrain tool in the current directory.

Approved request:
{request_json}

Inspect tool.py, test_tool.py, and README.md. Then run exactly:
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_tool.py

Reject the tool if tests fail, the implementation exceeds the approved capability, reads outside stdin, writes outside stdout, uses the network or subprocesses, silently ignores invalid input, or lacks a meaningful boundary test.

Return ONLY one JSON object:
{{
  "passed": true,
  "summary": "short evidence-based verdict",
  "checks": ["specific completed check"],
  "test_command": "PYTHONDONTWRITEBYTECODE=1 python3 -m unittest -v test_tool.py"
}}"#,
        );
        let audit = run_codex_turn(
            auditor_prompt,
            &[],
            None,
            build_directory.clone(),
            "You are ReelBrain Tool Auditor. You are independent from Toolsmith. Inspect the bounded artifact, run its tests in the read-only sandbox, and return strict JSON. A claim without a completed test command is a failure.",
            "reelbrain-tool-auditor",
            Duration::from_secs(120),
            "Tool Auditor",
            None,
        )?;
        let report = parse_agent_json(&audit.response)?;
        let passed = report.get("passed").and_then(Value::as_bool) == Some(true);
        let reported_test_command = report.get("test_command").and_then(Value::as_str);
        let test_executed = audit.activities.iter().any(|activity| {
            activity.kind == "command"
                && activity.status == "completed"
                && activity.detail.as_deref().is_some_and(|detail| {
                    detail.contains("unittest") || detail.contains("test_tool.py")
                })
        });
        if !passed || reported_test_command != Some(TOOL_AUDITOR_TEST_COMMAND) || !test_executed {
            return Err(
                "Independent tool audit did not produce passing executed-test evidence".into(),
            );
        }
        let summary = report
            .get("summary")
            .and_then(Value::as_str)
            .unwrap_or("Independent tests passed.")
            .to_owned();
        call_reelbrain_bridge(
            "tool_stage_generated",
            json!({
                "approval_id": request.approval_id,
                "tool_id": request.tool_name,
                "artifact_path": artifact,
                "capabilities": request.capabilities,
                "dependencies": request.dependencies,
            }),
            Duration::from_secs(20),
        )?;
        Ok((artifact, summary, report))
    })();

    match result {
        Ok((artifact, summary, report)) => {
            let bytes = fs::read(&artifact)
                .map_err(|error| format!("Could not digest generated tool: {error}"))?;
            request.status = "quarantined_pending_deploy_approval".into();
            request.build_path = Some(artifact.to_string_lossy().into_owned());
            request.artifact_digest = Some(format!("{:x}", Sha256::digest(bytes)));
            request.test_status = Some("passed".into());
            request.test_summary = Some(summary);
            request.auditor_report = Some(report);
            request.updated_at_ms = unix_time_ms();
            write_tool_approval_at(root, &request)?;
            append_tool_approval_audit(
                root,
                json!({
                    "eventType": "tool_build_test_passed",
                    "approvalId": request.approval_id,
                    "toolName": request.tool_name,
                    "artifactDigest": request.artifact_digest,
                    "actor": "agent:tool-auditor",
                    "atMs": request.updated_at_ms,
                }),
            )?;
        }
        Err(error) => {
            request.status = "build_or_test_failed".into();
            request.test_status = Some("failed".into());
            request.test_summary = Some(truncate_activity_detail(&error, 500));
            request.updated_at_ms = unix_time_ms();
            write_tool_approval_at(root, &request)?;
            append_tool_approval_audit(
                root,
                json!({
                    "eventType": "tool_build_test_failed",
                    "approvalId": request.approval_id,
                    "toolName": request.tool_name,
                    "reason": error,
                    "actor": "agent:tool-auditor",
                    "atMs": request.updated_at_ms,
                }),
            )?;
        }
    }
    Ok(request)
}

fn deploy_tested_tool_at(
    root: &Path,
    decision: ToolDeploymentDecisionRequest,
) -> Result<ToolApprovalRequest, String> {
    let mut request = load_tool_approval_at(root, &decision.approval_id)?;
    if request.status != "quarantined_pending_deploy_approval"
        || request.test_status.as_deref() != Some("passed")
    {
        return Err("Tool has not passed its independent quarantined audit".into());
    }
    let statement = validate_agent_profile_text(
        "Creator deployment statement",
        &decision.creator_statement,
        1000,
    )?;
    let now = unix_time_ms();
    if decision.decision == "deny" {
        request.status = "deployment_denied_by_creator".into();
        request.creator_statement = Some(statement.clone());
        request.updated_at_ms = now;
        write_tool_approval_at(root, &request)?;
        append_tool_approval_audit(
            root,
            json!({
                "eventType": "tool_deployment_denied",
                "approvalId": request.approval_id,
                "toolName": request.tool_name,
                "actor": "human:creator-founder",
                "creatorStatement": statement,
                "atMs": now,
            }),
        )?;
        return Ok(request);
    }
    if decision.decision != "approve" {
        return Err("Tool deployment decision must be approve or deny".into());
    }
    let receipt_payload = format!("deploy|{}|{}|{}", request.approval_id, now, statement);
    let receipt = format!("deploy_{:x}", Sha256::digest(receipt_payload.as_bytes()));
    let report = request
        .auditor_report
        .clone()
        .ok_or("Auditor report is missing")?;
    let deployed = call_reelbrain_bridge(
        "tool_deploy_approved",
        json!({
            "approval_id": request.approval_id,
            "approval_receipt_id": receipt,
            "auditor_report": report,
        }),
        Duration::from_secs(20),
    )?;
    request.status = "deployed".into();
    request.approval_receipt_id = Some(receipt);
    request.creator_statement = Some(statement.clone());
    request.deployed_tool_path = deployed
        .get("artifact_path")
        .and_then(Value::as_str)
        .map(str::to_owned);
    request.updated_at_ms = now;
    write_tool_approval_at(root, &request)?;
    append_tool_approval_audit(
        root,
        json!({
            "eventType": "tool_deployed_after_test",
            "approvalId": request.approval_id,
            "toolName": request.tool_name,
            "approvalReceiptId": request.approval_receipt_id,
            "artifactDigest": request.artifact_digest,
            "actor": "human:creator-founder",
            "creatorStatement": statement,
            "atMs": now,
        }),
    )?;
    Ok(request)
}

fn load_agent_profiles_at(root: &Path) -> Result<AgentProfileState, String> {
    let path = agent_profiles_path(root);
    if !path.is_file() {
        let defaults = default_agent_profiles();
        write_agent_profiles_at(root, &defaults)?;
        return Ok(defaults);
    }
    let state: AgentProfileState = serde_json::from_slice(
        &fs::read(&path).map_err(|error| format!("Could not read agent profiles: {error}"))?,
    )
    .map_err(|error| format!("Agent profile file is invalid: {error}"))?;
    let expected = [
        "meaning-scout",
        "hook-scout",
        "creator-advocate",
        "context-guardian",
    ];
    if state.profiles.len() != expected.len()
        || expected
            .iter()
            .any(|id| !state.profiles.iter().any(|profile| profile.id == *id))
    {
        return Err(
            "Agent profile file must contain exactly the four governed ReelBrain agent IDs".into(),
        );
    }
    let mut mention_keys = Vec::new();
    for profile in &state.profiles {
        validate_agent_profile_text("Agent name", &profile.name, 48)?;
        validate_agent_profile_text("Agent persona", &profile.role, 280)?;
        validate_agent_profile_text("Agent system prompt", &profile.system_prompt, 6000)?;
        let mention_key = agent_mention_key(&profile.name);
        if mention_key.is_empty() {
            return Err("Agent names must contain at least one letter or number".into());
        }
        if mention_keys.contains(&mention_key) {
            return Err("Agent names must produce unique @mention names".into());
        }
        mention_keys.push(mention_key);
    }
    Ok(state)
}

fn load_agent_profiles() -> Result<AgentProfileState, String> {
    load_agent_profiles_at(&workspace_root())
}

fn validate_agent_profile_text(label: &str, value: &str, maximum: usize) -> Result<String, String> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        return Err(format!("{label} is required"));
    }
    if trimmed.chars().count() > maximum {
        return Err(format!("{label} must be {maximum} characters or fewer"));
    }
    Ok(trimmed.to_owned())
}

fn agent_mention_key(value: &str) -> String {
    let mut result = String::new();
    let mut pending_separator = false;
    for character in value.trim().chars() {
        if character.is_alphanumeric() {
            if pending_separator && !result.is_empty() {
                result.push('-');
            }
            result.extend(character.to_lowercase());
            pending_separator = false;
        } else {
            pending_separator = true;
        }
    }
    result
}

fn update_agent_profile_at(
    root: &Path,
    request: UpdateAgentProfileRequest,
) -> Result<AgentProfileState, String> {
    let mut state = load_agent_profiles_at(root)?;
    if state.revision != request.expected_revision {
        return Err(format!(
            "Agent profiles changed from revision {} to {}; reload before saving",
            request.expected_revision, state.revision
        ));
    }
    let name = validate_agent_profile_text("Agent name", &request.name, 48)?;
    let mention_key = agent_mention_key(&name);
    if mention_key.is_empty() {
        return Err("Agent names must contain at least one letter or number".into());
    }
    if state
        .profiles
        .iter()
        .any(|profile| profile.id != request.id && agent_mention_key(&profile.name) == mention_key)
    {
        return Err("Agent names must produce unique @mention names".into());
    }
    let profile = state
        .profiles
        .iter_mut()
        .find(|profile| profile.id == request.id)
        .ok_or_else(|| format!("Unknown ReelBrain agent: {}", request.id))?;
    profile.name = name;
    profile.role = validate_agent_profile_text("Agent persona", &request.role, 280)?;
    profile.system_prompt =
        validate_agent_profile_text("Agent system prompt", &request.system_prompt, 6000)?;
    state.revision += 1;
    write_agent_profiles_at(root, &state)?;
    Ok(state)
}

fn resolve_python_bin() -> PathBuf {
    if let Some(path) = env::var_os("REELBRAIN_PYTHON_BIN") {
        return PathBuf::from(path);
    }
    let workspace = workspace_root();
    for candidate in [
        workspace.join(".venv/bin/python"),
        workspace.join("venv/bin/python"),
    ] {
        if candidate.is_file() {
            return candidate;
        }
    }
    PathBuf::from("python3")
}

fn call_reelbrain_bridge(
    command: &str,
    mut payload: Value,
    timeout: Duration,
) -> Result<Value, String> {
    let object = payload
        .as_object_mut()
        .ok_or("ReelBrain bridge payload must be an object")?;
    let workspace = workspace_root();
    object.insert(
        "workspace".into(),
        Value::String(workspace.to_string_lossy().into_owned()),
    );
    fs::create_dir_all(&workspace)
        .map_err(|error| format!("Unable to create ReelBrain data directory: {error}"))?;
    let mut python_paths = vec![python_module_root()];
    if let Some(existing) = env::var_os("PYTHONPATH") {
        python_paths.extend(env::split_paths(&existing));
    }
    let mut process = Command::new(resolve_python_bin());
    process
        .args(["-m", "reelbrain.desktop_bridge", command])
        .current_dir(&workspace)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Ok(python_path) = env::join_paths(python_paths) {
        process.env("PYTHONPATH", python_path);
    }
    let mut child = process
        .spawn()
        .map_err(|error| format!("Unable to start ReelBrain local service: {error}"))?;
    if let Some(mut stdin) = child.stdin.take() {
        serde_json::to_writer(&mut stdin, &payload)
            .map_err(|error| format!("Could not encode ReelBrain request: {error}"))?;
        stdin
            .flush()
            .map_err(|error| format!("Could not send ReelBrain request: {error}"))?;
    }
    let mut stdout = child.stdout.take().ok_or("ReelBrain stdout unavailable")?;
    let mut stderr = child.stderr.take().ok_or("ReelBrain stderr unavailable")?;
    let stdout_reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stdout.read_to_end(&mut bytes);
        (result, bytes)
    });
    let stderr_reader = thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stderr.read_to_end(&mut bytes);
        (result, bytes)
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = stdout_reader.join();
                let _ = stderr_reader.join();
                return Err(format!(
                    "ReelBrain local service timed out after {} seconds",
                    timeout.as_secs()
                ));
            }
            Err(error) => return Err(format!("Could not wait for ReelBrain service: {error}")),
        }
    };
    let (_, stdout_bytes) = stdout_reader
        .join()
        .map_err(|_| "ReelBrain stdout reader failed")?;
    let (_, stderr_bytes) = stderr_reader
        .join()
        .map_err(|_| "ReelBrain stderr reader failed")?;
    let stdout_text = String::from_utf8_lossy(&stdout_bytes);
    let stderr_text = String::from_utf8_lossy(&stderr_bytes);
    let response: Value = serde_json::from_str(stdout_text.trim()).map_err(|error| {
        format!(
            "ReelBrain local service returned invalid JSON: {error}. stderr: {}",
            stderr_text.trim()
        )
    })?;
    if response.get("ok").and_then(Value::as_bool) != Some(true) {
        let error = response.get("error").cloned().unwrap_or(Value::Null);
        return Err(format!("ReelBrain request failed: {error}"));
    }
    if !status.success() {
        return Err(format!(
            "ReelBrain local service exited unsuccessfully: {}",
            stderr_text.trim()
        ));
    }
    response
        .get("result")
        .cloned()
        .ok_or_else(|| "ReelBrain response did not contain a result".into())
}

fn account_status() -> Result<ConnectionStatus, String> {
    let mut server = AppServer::start()?;
    server.send(json!({
        "method": "account/read",
        "id": 2,
        "params": { "refreshToken": false }
    }))?;
    let result = server.read_response(2)?;
    let requires_openai_auth = result
        .get("requiresOpenaiAuth")
        .and_then(Value::as_bool)
        .unwrap_or(true);
    let account = result.get("account").filter(|value| !value.is_null());
    let auth_mode = account
        .and_then(|value| value.get("type"))
        .and_then(Value::as_str)
        .map(str::to_owned);
    let email = account
        .and_then(|value| value.get("email"))
        .and_then(Value::as_str)
        .map(str::to_owned);
    let plan_type = account
        .and_then(|value| value.get("planType"))
        .and_then(Value::as_str)
        .map(str::to_owned);
    let connected = account.is_some() || !requires_openai_auth;
    let detail = if connected {
        match (&auth_mode, &email) {
            (Some(mode), Some(email)) => format!("Connected with {mode} as {email}"),
            (Some(mode), None) => format!("Connected with {mode}"),
            _ => "Codex is ready".into(),
        }
    } else {
        "Sign in through the official Codex browser flow".into()
    };
    Ok(ConnectionStatus {
        connected,
        auth_mode,
        email,
        plan_type,
        requires_openai_auth,
        detail,
    })
}

type ActivitySink = Arc<dyn Fn(ChatActivity) + Send + Sync>;

fn truncate_activity_detail(value: &str, maximum: usize) -> String {
    let normalized = value.split_whitespace().collect::<Vec<_>>().join(" ");
    if normalized.chars().count() <= maximum {
        return normalized;
    }
    let mut result = normalized
        .chars()
        .take(maximum.saturating_sub(1))
        .collect::<String>();
    result.push('…');
    result
}

fn command_activity_detail(command: &str) -> String {
    let mut redact_next = false;
    let sanitized = command
        .split_whitespace()
        .take(18)
        .map(|token| {
            if redact_next {
                redact_next = false;
                return "••••".to_owned();
            }
            let lowered = token.to_ascii_lowercase();
            let sensitive = [
                "token", "secret", "password", "api_key", "api-key", "apikey",
            ]
            .iter()
            .any(|needle| lowered.contains(needle));
            if !sensitive {
                return token.to_owned();
            }
            if let Some((name, _)) = token.split_once('=') {
                return format!("{name}=••••");
            }
            redact_next = true;
            token.to_owned()
        })
        .collect::<Vec<_>>()
        .join(" ");
    truncate_activity_detail(&sanitized, 180)
}

fn argument_shape(value: Option<&Value>) -> Option<String> {
    match value {
        Some(Value::Object(object)) if !object.is_empty() => Some(format!(
            "arguments: {}",
            object
                .keys()
                .take(6)
                .cloned()
                .collect::<Vec<_>>()
                .join(", ")
        )),
        Some(Value::Array(items)) => Some(format!("{} argument item(s)", items.len())),
        Some(value) if !value.is_null() => Some("arguments supplied".into()),
        _ => None,
    }
}

fn item_activity(item: &Value, actor: &str, lifecycle: &str) -> Option<ChatActivity> {
    let kind = item.get("type")?.as_str()?;
    let id = item.get("id")?.as_str()?.to_owned();
    let raw_status = item.get("status").and_then(Value::as_str);
    let status = match raw_status {
        Some("failed" | "declined") => "failed",
        Some("completed") => "completed",
        _ if lifecycle == "completed" => "completed",
        _ => "running",
    }
    .to_owned();
    let (activity_kind, title, detail) = match kind {
        "commandExecution" => {
            let command = item
                .get("command")
                .and_then(Value::as_str)
                .unwrap_or("command");
            (
                "command",
                format!(
                    "Run {}",
                    command.split_whitespace().next().unwrap_or("command")
                ),
                Some(command_activity_detail(command)),
            )
        }
        "fileChange" => {
            let changes = item.get("changes").and_then(Value::as_array);
            let paths = changes
                .into_iter()
                .flatten()
                .filter_map(|change| change.get("path").and_then(Value::as_str))
                .take(3)
                .collect::<Vec<_>>();
            (
                "file",
                "Prepare file changes".into(),
                (!paths.is_empty()).then(|| truncate_activity_detail(&paths.join(", "), 180)),
            )
        }
        "mcpToolCall" => {
            let server = item.get("server").and_then(Value::as_str).unwrap_or("MCP");
            let tool = item.get("tool").and_then(Value::as_str).unwrap_or("tool");
            (
                "tool",
                format!("Call {tool}"),
                Some(
                    [
                        Some(format!("{server} server")),
                        argument_shape(item.get("arguments")),
                    ]
                    .into_iter()
                    .flatten()
                    .collect::<Vec<_>>()
                    .join(" · "),
                ),
            )
        }
        "dynamicToolCall" => {
            let namespace = item.get("namespace").and_then(Value::as_str);
            let tool = item.get("tool").and_then(Value::as_str).unwrap_or("tool");
            (
                "tool",
                format!("Call {tool}"),
                Some(
                    [
                        namespace.map(|value| format!("{value} namespace")),
                        argument_shape(item.get("arguments")),
                    ]
                    .into_iter()
                    .flatten()
                    .collect::<Vec<_>>()
                    .join(" · "),
                ),
            )
        }
        "collabAgentToolCall" => {
            let tool = item
                .get("tool")
                .and_then(Value::as_str)
                .unwrap_or("coordinate");
            (
                "team",
                match tool {
                    "spawnAgent" => "Start collaborator",
                    "sendInput" => "Steer collaborator",
                    "wait" => "Wait for collaborators",
                    "closeAgent" => "Close collaborator",
                    _ => "Coordinate collaborators",
                }
                .into(),
                item.get("prompt")
                    .and_then(Value::as_str)
                    .map(|value| truncate_activity_detail(value, 180)),
            )
        }
        "subAgentActivity" => (
            "team",
            "Collaborator activity".into(),
            item.get("agentPath")
                .and_then(Value::as_str)
                .map(|value| truncate_activity_detail(value, 180)),
        ),
        "webSearch" => (
            "search",
            "Search the web".into(),
            item.get("query")
                .and_then(Value::as_str)
                .map(|value| truncate_activity_detail(value, 180)),
        ),
        "imageView" => (
            "tool",
            "Inspect image".into(),
            item.get("path")
                .and_then(Value::as_str)
                .map(|value| truncate_activity_detail(value, 180)),
        ),
        "imageGeneration" => ("tool", "Generate image".into(), None),
        _ => return None,
    };
    Some(ChatActivity {
        id,
        actor: actor.to_owned(),
        kind: activity_kind.into(),
        title,
        detail: detail.filter(|value| !value.is_empty()),
        status,
    })
}

fn activity_sink(app: tauri::AppHandle, request_id: String) -> ActivitySink {
    Arc::new(move |activity| {
        let _ = app.emit(
            "chat-activity",
            ChatActivityEvent {
                request_id: request_id.clone(),
                activity,
            },
        );
    })
}

fn publish_activity(sink: &Option<ActivitySink>, activity: &ChatActivity) {
    if let Some(sink) = sink {
        sink(activity.clone());
    }
}

fn canonical_chat_image(path: &str) -> Result<String, String> {
    let canonical = Path::new(path)
        .canonicalize()
        .map_err(|error| format!("Cannot access attached image: {error}"))?;
    let metadata = canonical
        .metadata()
        .map_err(|error| format!("Cannot inspect attached image: {error}"))?;
    if !metadata.is_file() || metadata.len() > 20 * 1024 * 1024 {
        return Err("Attached images must be local files no larger than 20 MB".into());
    }
    let extension = canonical
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if !matches!(extension.as_str(), "png" | "jpg" | "jpeg" | "webp" | "gif") {
        return Err(format!("Unsupported chat image type: .{extension}"));
    }
    Ok(canonical.to_string_lossy().into_owned())
}

fn run_codex_turn_with_sandbox(
    prompt: String,
    image_paths: &[String],
    thread_id: Option<String>,
    cwd: PathBuf,
    sandbox: &str,
    base_instructions: &str,
    service_name: &str,
    timeout: Duration,
    actor: &str,
    activity_sink: Option<ActivitySink>,
) -> Result<ChatResult, String> {
    if prompt.trim().is_empty() {
        return Err("A creator message is required".into());
    }
    let mut server = AppServer::start_with_timeout(timeout)?;
    let thread_result = if let Some(thread_id) = thread_id.as_ref() {
        server.send(json!({
            "method": "thread/resume",
            "id": 2,
            "params": {
                "threadId": thread_id,
                "cwd": cwd,
                "sandbox": sandbox,
                "approvalPolicy": "never",
                "baseInstructions": base_instructions
            }
        }))?;
        server.read_response(2)?
    } else {
        server.send(json!({
            "method": "thread/start",
            "id": 2,
            "params": {
                "cwd": cwd,
                "sandbox": sandbox,
                "approvalPolicy": "never",
                "baseInstructions": base_instructions,
                "serviceName": service_name
            }
        }))?;
        server.read_response(2)?
    };
    let thread_id = thread_result
        .get("thread")
        .and_then(|thread| thread.get("id"))
        .and_then(Value::as_str)
        .ok_or("Codex did not return a thread id")?
        .to_owned();
    let mut input = vec![json!({
        "type": "text",
        "text": prompt,
        "text_elements": []
    })];
    for path in image_paths {
        input.push(json!({
            "type": "localImage",
            "path": canonical_chat_image(path)?,
            "detail": "auto"
        }));
    }
    server.send(json!({
        "method": "turn/start",
        "id": 3,
        "params": {
            "threadId": thread_id,
            "input": input
        }
    }))?;
    server.read_response(3)?;
    let mut response = String::new();
    let mut activity_order = Vec::new();
    let mut activities = HashMap::<String, ChatActivity>::new();
    loop {
        let message = server.read_json()?;
        match message.get("method").and_then(Value::as_str) {
            Some("item/agentMessage/delta") => {
                if let Some(delta) = message
                    .get("params")
                    .and_then(|params| params.get("delta"))
                    .and_then(Value::as_str)
                {
                    response.push_str(delta);
                }
            }
            Some(method @ ("item/started" | "item/completed")) => {
                let lifecycle = if method == "item/completed" {
                    "completed"
                } else {
                    "started"
                };
                if let Some(activity) = message
                    .get("params")
                    .and_then(|params| params.get("item"))
                    .and_then(|item| item_activity(item, actor, lifecycle))
                {
                    let key = format!("{}:{}", activity.actor, activity.id);
                    if !activities.contains_key(&key) {
                        activity_order.push(key.clone());
                    }
                    activities.insert(key, activity.clone());
                    publish_activity(&activity_sink, &activity);
                }
            }
            Some("item/mcpToolCall/progress") => {
                let params = message.get("params").unwrap_or(&Value::Null);
                if let Some(item_id) = params.get("itemId").and_then(Value::as_str) {
                    let key = format!("{actor}:{item_id}");
                    if let Some(activity) = activities.get_mut(&key) {
                        activity.detail = params
                            .get("message")
                            .and_then(Value::as_str)
                            .map(|value| truncate_activity_detail(value, 180));
                        publish_activity(&activity_sink, activity);
                    }
                }
            }
            Some("turn/completed") => break,
            Some("error") => {
                return Err(format!(
                    "Codex turn failed: {}",
                    message.get("params").cloned().unwrap_or(Value::Null)
                ));
            }
            _ => {}
        }
    }
    if response.trim().is_empty() {
        response = "I finished the turn, but no creator-facing message was returned.".into();
    }
    Ok(ChatResult {
        thread_id,
        response: response.trim().to_owned(),
        activities: activity_order
            .into_iter()
            .filter_map(|key| activities.remove(&key))
            .collect(),
        approval_request: None,
        revision_proposal: None,
    })
}

fn run_codex_turn(
    prompt: String,
    image_paths: &[String],
    thread_id: Option<String>,
    cwd: PathBuf,
    base_instructions: &str,
    service_name: &str,
    timeout: Duration,
    actor: &str,
    activity_sink: Option<ActivitySink>,
) -> Result<ChatResult, String> {
    run_codex_turn_with_sandbox(
        prompt,
        image_paths,
        thread_id,
        cwd,
        "read-only",
        base_instructions,
        service_name,
        timeout,
        actor,
        activity_sink,
    )
}

fn run_chat(request: ChatRequest, sink: Option<ActivitySink>) -> Result<ChatResult, String> {
    let profiles = load_agent_profiles()?.profiles;
    let profile_context = serde_json::to_string_pretty(&profiles)
        .map_err(|error| format!("Could not encode ReelBrain agent profiles: {error}"))?;
    let context = request
        .context
        .unwrap_or_else(|| "No project context was supplied.".into());
    let prompt = format!(
        r#"Creator request:
{creator_request}

Current project context:
{context}

Respond as ReelBrain's Showrunner."#,
        creator_request = request.prompt,
        context = context,
    );
    let instructions = format!(
        r#"{REELBRAIN_INSTRUCTIONS}

{TOOL_GOVERNANCE_INSTRUCTIONS}

You are ReelBrain's Showrunner. Judge the request semantically yourself; there is no keyword classifier. Answer directly when collaboration would not help. When the request benefits from independent editing perspectives, use Codex collaboration/subagent tools and orchestrate the relevant configured agents. Explain why you consulted them through the normal tool activity trace. Do not wait for a frontend router to make this decision.

Exactly four creator-configured editing personas are available:
{profile_context}

Approval and denial happen only through visible Yes/No controls. Never treat typed conversational text as authorization.

If your response contains a concrete actionable revision proposal that should be offered for creator approval, append exactly one marker at the very end:
<reelbrain-revision-proposal>{{"summary":"concise description of the proposed revision"}}</reelbrain-revision-proposal>

Do not append that marker for explanations, status questions, evidence inspection, brainstorming, or an effect that already completed. The marker only asks the desktop app to show Yes/No; it does not authorize or execute the revision."#,
    );
    let mut result = run_codex_turn(
        prompt,
        &request.image_paths,
        request.thread_id,
        request
            .cwd
            .map(PathBuf::from)
            .unwrap_or_else(workspace_root),
        &instructions,
        "reelbrain-desktop",
        Duration::from_secs(90),
        "ReelBrain Showrunner",
        sink,
    )?;
    let (response, revision_proposal) = extract_revision_proposal(&result.response)?;
    let (response, approval_request) =
        extract_tool_approval_request_at(&workspace_root(), &response, "ReelBrain Showrunner")?;
    result.response = response;
    result.approval_request = approval_request;
    result.revision_proposal = if result.approval_request.is_none() {
        revision_proposal
    } else {
        None
    };
    Ok(result)
}

fn run_persona_chat(
    request: PersonaChatRequest,
    sink: Option<ActivitySink>,
) -> Result<ChatResult, String> {
    let profile = load_agent_profiles()?
        .profiles
        .into_iter()
        .find(|profile| profile.id == request.persona_id)
        .ok_or_else(|| format!("Unknown ReelBrain agent: {}", request.persona_id))?;
    let actor = profile.name.clone();
    let context = request
        .context
        .unwrap_or_else(|| "No project context was supplied.".into());
    let prompt = format!(
        r#"The creator directly mentioned you in ReelBrain.

Creator request:
{creator_request}

Current project context:
{context}

Respond as an editing collaborator. Give a concrete proposed edit or revision and explain the reason briefly. Do not claim that an edit was rendered, saved, approved, or published. Do not invent source evidence."#,
        creator_request = request.prompt.trim(),
        context = context,
    );
    let instructions = format!(
        "You are ReelBrain's {name}. Persona: {role}\n\nCreator-configured system prompt:\n{system_prompt}\n\nMemory is a behavioral prior, never source evidence. Current creator steering outranks stored preferences. You are read-only unless a separately governed effect is approved.",
        name = profile.name,
        role = profile.role,
        system_prompt = profile.system_prompt,
    );
    run_codex_turn(
        prompt,
        &request.image_paths,
        request.thread_id,
        workspace_root(),
        &instructions,
        &format!("reelbrain-{}", profile.id),
        Duration::from_secs(120),
        &actor,
        sink,
    )
}

fn run_team_chat(
    request: TeamChatRequest,
    sink: Option<ActivitySink>,
) -> Result<TeamChatResult, String> {
    let profiles = load_agent_profiles()?.profiles;
    let context = request
        .context
        .unwrap_or_else(|| "No project context was supplied.".into());
    let creator_prompt = request.prompt.trim().to_owned();
    let image_paths = request.image_paths.clone();
    if creator_prompt.is_empty() {
        return Err("A creator message is required".into());
    }

    let handles = profiles
        .iter()
        .cloned()
        .map(|profile| {
            let context = context.clone();
            let creator_prompt = creator_prompt.clone();
            let image_paths = image_paths.clone();
            let sink = sink.clone();
            thread::spawn(move || {
                let actor = profile.name.clone();
                let consultation_id = format!("team-consult-{}", profile.id);
                let started = ChatActivity {
                    id: consultation_id.clone(),
                    actor: "ReelBrain Showrunner".into(),
                    kind: "team".into(),
                    title: format!("Consult {}", profile.name),
                    detail: Some(profile.role.clone()),
                    status: "running".into(),
                };
                publish_activity(&sink, &started);
                let prompt = format!(
                    r#"The ReelBrain Showrunner is consulting all four editors.

Creator request:
{creator_prompt}

Current project context:
{context}

Return your independent editing recommendation. Be concrete about what should change, preserve, or be checked. Do not claim an edit was executed and do not invent source evidence."#,
                );
                let instructions = format!(
                    "You are ReelBrain's {name}. Persona: {role}\n\nCreator-configured system prompt:\n{system_prompt}\n\nYou are one independent read-only editing agent. Memory is a behavioral prior, never source evidence.",
                    name = profile.name,
                    role = profile.role,
                    system_prompt = profile.system_prompt,
                );
                let turn = match run_codex_turn(
                    prompt,
                    &image_paths,
                    None,
                    workspace_root(),
                    &instructions,
                    &format!("reelbrain-{}", profile.id),
                    Duration::from_secs(120),
                    &actor,
                    sink.clone(),
                ) {
                    Ok(turn) => turn,
                    Err(error) => {
                        let failed = ChatActivity {
                            status: "failed".into(),
                            detail: Some(truncate_activity_detail(&error, 180)),
                            ..started
                        };
                        publish_activity(&sink, &failed);
                        return Err(error);
                    }
                };
                let completed = ChatActivity {
                    status: "completed".into(),
                    ..started
                };
                publish_activity(&sink, &completed);
                Ok::<_, String>((profile, turn, completed))
            })
        })
        .collect::<Vec<_>>();

    let mut contributions = Vec::new();
    let mut agent_thread_ids = Vec::new();
    let mut participants = Vec::new();
    let mut activities = Vec::new();
    for handle in handles {
        let (profile, turn, consultation) = handle
            .join()
            .map_err(|_| "A ReelBrain editing agent thread panicked".to_string())??;
        participants.push(profile.name.clone());
        agent_thread_ids.push(turn.thread_id.clone());
        activities.push(consultation);
        activities.extend(turn.activities.clone());
        contributions.push(json!({
            "agentId": profile.id,
            "agentName": profile.name,
            "role": profile.role,
            "recommendation": turn.response,
        }));
    }

    let synthesis_prompt = format!(
        r#"The creator asked ReelBrain's editing team:
{creator_prompt}

Project context:
{context}

The four independent editing agents returned:
{contributions}

Synthesize one coherent creator-facing revision plan. Call out meaningful disagreements. State which agent should own each next step. Prefer short headings and bullets; do not use Markdown tables because this appears in a narrow chat panel. Do not claim rendering, saving, approval, publishing, or any other effect occurred."#,
        contributions = serde_json::to_string_pretty(&contributions)
            .map_err(|error| format!("Could not encode team responses: {error}"))?,
    );
    let synthesis_started = ChatActivity {
        id: "team-synthesis".into(),
        actor: "ReelBrain Showrunner".into(),
        kind: "team".into(),
        title: "Synthesize four recommendations".into(),
        detail: Some(
            "Resolve disagreements and produce one coherent creator-facing revision plan.".into(),
        ),
        status: "running".into(),
    };
    publish_activity(&sink, &synthesis_started);
    let root_instructions = format!(
        "{REELBRAIN_INSTRUCTIONS}\n\n{TOOL_GOVERNANCE_INSTRUCTIONS}\n\nYou are ReelBrain's Showrunner. You orchestrate exactly four configurable editing agents, synthesize their independent recommendations, expose disagreement, and never imply that consultation or effects occurred unless they actually did."
    );
    let mut root = match run_codex_turn(
        synthesis_prompt,
        &image_paths,
        request.thread_id,
        workspace_root(),
        &root_instructions,
        "reelbrain-showrunner",
        Duration::from_secs(120),
        "ReelBrain Showrunner",
        sink.clone(),
    ) {
        Ok(root) => root,
        Err(error) => {
            let failed = ChatActivity {
                status: "failed".into(),
                detail: Some(truncate_activity_detail(&error, 180)),
                ..synthesis_started
            };
            publish_activity(&sink, &failed);
            return Err(error);
        }
    };
    let (clean_response, approval_request) = extract_tool_approval_request_at(
        &workspace_root(),
        &root.response,
        "ReelBrain Showrunner",
    )?;
    root.response = clean_response;
    root.approval_request = approval_request;
    let synthesis_completed = ChatActivity {
        status: "completed".into(),
        ..synthesis_started
    };
    publish_activity(&sink, &synthesis_completed);
    activities.push(synthesis_completed);
    activities.extend(root.activities.clone());
    Ok(TeamChatResult {
        thread_id: root.thread_id,
        response: root.response,
        activities,
        agent_thread_ids,
        participants,
        approval_request: root.approval_request,
    })
}

fn parse_agent_json(response: &str) -> Result<Value, String> {
    if let Ok(value) = serde_json::from_str::<Value>(response.trim()) {
        return Ok(value);
    }
    let start = response
        .find('{')
        .ok_or("Editorial agent did not return a JSON object")?;
    let end = response
        .rfind('}')
        .ok_or("Editorial agent returned incomplete JSON")?;
    serde_json::from_str(&response[start..=end])
        .map_err(|error| format!("Editorial agent returned invalid JSON: {error}"))
}

fn persona_display_name(persona: &str) -> &str {
    match persona {
        "meaning-scout" => "Story Editor",
        "hook-scout" => "Retention Editor",
        "creator-advocate" => "Style Editor",
        "context-guardian" => "Continuity Editor",
        _ => persona,
    }
}

fn run_persona_agent(
    app: tauri::AppHandle,
    fanout_id: String,
    task: Value,
) -> Result<(Value, String), String> {
    let persona = task
        .get("persona")
        .and_then(Value::as_str)
        .ok_or("Fan-out task persona is missing")?
        .to_owned();
    let task_id = task
        .get("task_id")
        .and_then(Value::as_str)
        .ok_or("Fan-out task id is missing")?
        .to_owned();
    let token = task
        .get("capability_packet")
        .and_then(|value| value.get("token"))
        .and_then(Value::as_str)
        .ok_or("Fan-out capability token is missing")?;
    let _ = app.emit(
        "fanout-progress",
        AgentProgress {
            fanout_id: fanout_id.clone(),
            persona: persona.clone(),
            status: "authorizing".into(),
            detail: "Requesting bounded ReelBrain context".into(),
            thread_id: None,
        },
    );
    let context = call_reelbrain_bridge(
        "fanout_context",
        json!({
            "fanout_id": fanout_id,
            "task_id": task_id,
            "capability_token": token,
        }),
        Duration::from_secs(15),
    )?;
    let _ = app.emit(
        "fanout-progress",
        AgentProgress {
            fanout_id: fanout_id.clone(),
            persona: persona.clone(),
            status: "running".into(),
            detail: "Independent Codex persona thread is reviewing grounded candidates".into(),
            thread_id: None,
        },
    );
    let instruction = task
        .get("instruction")
        .and_then(Value::as_str)
        .unwrap_or("Review the supplied candidates.");
    let configured_profile = load_agent_profiles().ok().and_then(|state| {
        state
            .profiles
            .into_iter()
            .find(|profile| profile.id == persona)
    });
    let display_name = configured_profile
        .as_ref()
        .map(|profile| profile.name.as_str())
        .unwrap_or_else(|| persona_display_name(&persona));
    let configured_role = configured_profile
        .as_ref()
        .map(|profile| profile.role.as_str())
        .unwrap_or("Independent editorial assessment");
    let configured_prompt = configured_profile
        .as_ref()
        .map(|profile| profile.system_prompt.as_str())
        .unwrap_or("Use the supplied governed instruction and source-grounded candidate catalog.");
    let prompt = format!(
        r#"You are the {display_name} in ReelBrain's governed editorial team.

Your task: {instruction}

Creator-configured persona: {configured_role}

Creator-configured system prompt:
{configured_prompt}

Memory is a behavioral prior, never source evidence. Current creator steering in the authorized context outranks stored preferences. Use only candidates in the authorized context. Do not invent timestamps, transcript claims, candidates, approvals, or effects.

Return ONLY one JSON object with this exact shape:
{{
  "selections": [
    {{
      "candidate_id": "one authorized candidate_id",
      "score": 0.0,
      "rationale": "concise evidence-grounded reason",
      "risks": ["specific risk"],
      "used_preference_ids": ["only preference IDs actually applied"]
    }}
  ]
}}

Select between 1 and 5 candidates. Scores must be between 0 and 1.

Authorized context:
{context}"#,
        display_name = display_name,
        instruction = instruction,
        configured_role = configured_role,
        configured_prompt = configured_prompt,
        context = serde_json::to_string_pretty(&context)
            .map_err(|error| format!("Could not encode persona context: {error}"))?,
    );
    let base_instructions = format!(
        "You are ReelBrain's {display_name}. Persona: {configured_role}\n\nCreator-configured system prompt:\n{configured_prompt}\n\nYou are an independent read-only editorial subagent. Follow the supplied capability-bounded context and return strict JSON only."
    );
    let turn = run_codex_turn(
        prompt,
        &[],
        None,
        workspace_root(),
        &base_instructions,
        &format!("reelbrain-{persona}"),
        Duration::from_secs(120),
        display_name,
        None,
    );
    match turn {
        Ok(turn) => {
            let agent_json = parse_agent_json(&turn.response)?;
            let selections = agent_json
                .get("selections")
                .cloned()
                .ok_or("Editorial agent response is missing selections")?;
            let result = json!({
                "task_id": task.get("task_id").cloned().unwrap_or(Value::Null),
                "persona": persona,
                "epoch": task.get("epoch").cloned().unwrap_or(Value::Null),
                "snapshot_digest": task.get("snapshot_digest").cloned().unwrap_or(Value::Null),
                "memory_snapshot_digest": task.get("memory_snapshot_digest").cloned().unwrap_or(Value::Null),
                "selections": selections,
            });
            let _ = app.emit(
                "fanout-progress",
                AgentProgress {
                    fanout_id,
                    persona: result
                        .get("persona")
                        .and_then(Value::as_str)
                        .unwrap_or("agent")
                        .to_owned(),
                    status: "completed".into(),
                    detail: format!(
                        "Returned {} grounded selection(s)",
                        result
                            .get("selections")
                            .and_then(Value::as_array)
                            .map(Vec::len)
                            .unwrap_or(0)
                    ),
                    thread_id: Some(turn.thread_id.clone()),
                },
            );
            Ok((result, turn.thread_id))
        }
        Err(error) => {
            let _ = app.emit(
                "fanout-progress",
                AgentProgress {
                    fanout_id,
                    persona,
                    status: "failed".into(),
                    detail: error.clone(),
                    thread_id: None,
                },
            );
            Err(error)
        }
    }
}

fn run_editorial_fanout(
    app: tauri::AppHandle,
    request: FanoutStartRequest,
) -> Result<Value, String> {
    let plan = call_reelbrain_bridge(
        "fanout_plan",
        json!({
            "source_path": request.source_path,
            "source_sha256": request.source_sha256,
            "project_id": request.project_id.unwrap_or_else(|| "desktop-project".into()),
            "creator_id": request.creator_id.unwrap_or_else(|| "creator-founder".into()),
            "current_steering": request.current_steering,
        }),
        Duration::from_secs(20),
    )?;
    if plan.get("status").and_then(Value::as_str) != Some("READY_FOR_HOST_DISPATCH") {
        return Ok(plan);
    }
    let fanout_id = plan
        .get("fanout_id")
        .and_then(Value::as_str)
        .ok_or("ReelBrain fan-out id is missing")?
        .to_owned();
    let tasks = plan
        .get("tasks")
        .and_then(Value::as_array)
        .ok_or("ReelBrain fan-out tasks are missing")?
        .clone();
    let handles = tasks
        .into_iter()
        .map(|task| {
            let app = app.clone();
            let fanout_id = fanout_id.clone();
            thread::spawn(move || run_persona_agent(app, fanout_id, task))
        })
        .collect::<Vec<_>>();
    let mut results = Vec::new();
    let mut threads = Vec::new();
    let mut failures = Vec::new();
    for handle in handles {
        match handle.join() {
            Ok(Ok((result, thread_id))) => {
                results.push(result);
                threads.push(thread_id);
            }
            Ok(Err(error)) => failures.push(error),
            Err(_) => failures.push("Editorial agent thread panicked".into()),
        }
    }
    if !failures.is_empty() {
        return Err(format!(
            "Editorial fan-out did not complete: {}",
            failures.join(" | ")
        ));
    }
    let root_token = plan
        .get("root_authority")
        .and_then(|value| value.get("token"))
        .and_then(Value::as_str)
        .ok_or("Root fan-out authority is missing")?;
    let submitted = call_reelbrain_bridge(
        "fanout_submit",
        json!({
            "fanout_id": fanout_id,
            "root_capability_token": root_token,
            "results": results,
        }),
        Duration::from_secs(20),
    )?;
    Ok(json!({
        "status": submitted.get("status").cloned().unwrap_or(Value::Null),
        "fanoutId": fanout_id,
        "epoch": submitted.get("epoch").cloned().unwrap_or(Value::Null),
        "evidenceRevision": submitted.get("evidence_revision").cloned().unwrap_or(Value::Null),
        "planDigest": submitted.get("plan_digest").cloned().unwrap_or(Value::Null),
        "planPath": submitted.get("plan_path").cloned().unwrap_or(Value::Null),
        "selectedCandidateIds": submitted.get("selected_candidate_ids").cloned().unwrap_or(Value::Null),
        "creatorReviewRequired": true,
        "publishReady": false,
        "rootAuthorityToken": root_token,
        "agentThreadIds": threads,
        "agentResults": results,
    }))
}

fn digest_file(path: &Path) -> Result<String, String> {
    let mut file = File::open(path).map_err(|error| format!("Cannot open source: {error}"))?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let count = file
            .read(&mut buffer)
            .map_err(|error| format!("Cannot hash source: {error}"))?;
        if count == 0 {
            break;
        }
        hasher.update(&buffer[..count]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn probe_video(path: &Path) -> Option<Value> {
    let output = Command::new("ffprobe")
        .args([
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type,width,height",
            "-of",
            "json",
        ])
        .arg(path)
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    serde_json::from_slice(&output.stdout).ok()
}

fn inspect_video(path: &Path) -> Result<VideoPreflight, String> {
    let resolved = path
        .canonicalize()
        .map_err(|error| format!("Cannot access selected file: {error}"))?;
    let metadata = resolved
        .metadata()
        .map_err(|error| format!("Cannot inspect selected file: {error}"))?;
    if !metadata.is_file() {
        return Err("The selected path is not a file".into());
    }
    let name = resolved
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("video")
        .to_owned();
    let probe = probe_video(&resolved);
    let streams = probe
        .as_ref()
        .and_then(|value| value.get("streams"))
        .and_then(Value::as_array);
    let video_stream = streams.and_then(|items| {
        items
            .iter()
            .find(|item| item.get("codec_type").and_then(Value::as_str) == Some("video"))
    });
    let has_video = video_stream.is_some();
    let has_audio = streams
        .map(|items| {
            items
                .iter()
                .any(|item| item.get("codec_type").and_then(Value::as_str) == Some("audio"))
        })
        .unwrap_or(false);
    let duration_seconds = probe
        .as_ref()
        .and_then(|value| value.get("format"))
        .and_then(|format| format.get("duration"))
        .and_then(Value::as_str)
        .and_then(|value| value.parse::<f64>().ok());
    let width = video_stream
        .and_then(|stream| stream.get("width"))
        .and_then(Value::as_u64);
    let height = video_stream
        .and_then(|stream| stream.get("height"))
        .and_then(Value::as_u64);
    let ready = has_video && has_audio;
    Ok(VideoPreflight {
        path: resolved.to_string_lossy().into_owned(),
        name,
        size_bytes: metadata.len(),
        sha256: digest_file(&resolved)?,
        duration_seconds,
        width,
        height,
        has_video,
        has_audio,
        status: if ready { "ready" } else { "unsupported" }.into(),
        message: if ready {
            "Ready for a governed edit plan".into()
        } else if !has_video {
            "No video stream was detected".into()
        } else {
            "No audio stream was detected".into()
        },
    })
}

fn persist_chat_image_at(root: &Path, request: PersistChatImageRequest) -> Result<String, String> {
    if request.bytes.is_empty() || request.bytes.len() > 20 * 1024 * 1024 {
        return Err("Pasted images must be between 1 byte and 20 MB".into());
    }
    let extension = match request.mime_type.as_str() {
        "image/png" => "png",
        "image/jpeg" | "image/jpg" => "jpg",
        "image/webp" => "webp",
        "image/gif" => "gif",
        _ => request
            .name
            .rsplit_once('.')
            .map(|(_, extension)| extension)
            .filter(|extension| {
                matches!(
                    extension.to_ascii_lowercase().as_str(),
                    "png" | "jpg" | "jpeg" | "webp" | "gif"
                )
            })
            .ok_or("Clipboard image type is unsupported")?,
    }
    .to_ascii_lowercase();
    let digest = format!("{:x}", Sha256::digest(&request.bytes));
    let directory = root.join(".reelbrain/desktop/chat-attachments");
    fs::create_dir_all(&directory)
        .map_err(|error| format!("Could not create chat attachment directory: {error}"))?;
    let path = directory.join(format!("{digest}.{extension}"));
    if !path.is_file() {
        let temporary = directory.join(format!(".{digest}.{}.tmp", std::process::id()));
        fs::write(&temporary, request.bytes)
            .map_err(|error| format!("Could not store pasted image: {error}"))?;
        fs::rename(&temporary, &path)
            .map_err(|error| format!("Could not commit pasted image: {error}"))?;
    }
    Ok(path.to_string_lossy().into_owned())
}

fn prepare_chat_image_at(path: &Path) -> Result<String, String> {
    if !path.is_file() {
        return Err("Chat image does not exist".into());
    }
    let metadata =
        fs::metadata(path).map_err(|error| format!("Could not inspect chat image: {error}"))?;
    if metadata.len() == 0 || metadata.len() > 20 * 1024 * 1024 {
        return Err("Chat images must be between 1 byte and 20 MB".into());
    }
    let mime = match path
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("png") => "image/png",
        Some("jpg") | Some("jpeg") => "image/jpeg",
        Some("webp") => "image/webp",
        Some("gif") => "image/gif",
        _ => return Err("Chat image type is unsupported".into()),
    };
    let bytes = fs::read(path).map_err(|error| format!("Could not read chat image: {error}"))?;
    Ok(format!("data:{mime};base64,{}", BASE64.encode(bytes)))
}

fn find_latest_manifest(root: &Path) -> Option<PathBuf> {
    let dogfood = root.join(".reelbrain/dogfood");
    let mut candidates = fs::read_dir(dogfood)
        .ok()?
        .filter_map(Result::ok)
        .map(|entry| entry.path().join("run/run_manifest.json"))
        .filter(|path| path.is_file())
        .collect::<Vec<_>>();
    candidates.sort();
    candidates.pop()
}

fn image_data_url(path: &str) -> Option<String> {
    let bytes = fs::read(path).ok()?;
    let mime = match Path::new(path)
        .extension()
        .and_then(|value| value.to_str())
        .map(str::to_ascii_lowercase)
        .as_deref()
    {
        Some("png") => "image/png",
        Some("webp") => "image/webp",
        _ => "image/jpeg",
    };
    Some(format!("data:{mime};base64,{}", BASE64.encode(bytes)))
}

fn parse_srt_seconds(value: &str) -> Option<f64> {
    let normalized = value.trim().replace(',', ".");
    let mut parts = normalized.split(':');
    let hours = parts.next()?.parse::<f64>().ok()?;
    let minutes = parts.next()?.parse::<f64>().ok()?;
    let seconds = parts.next()?.parse::<f64>().ok()?;
    Some(hours * 3600.0 + minutes * 60.0 + seconds)
}

fn caption_preview(path: &str) -> Vec<CaptionPreview> {
    let Ok(text) = fs::read_to_string(path) else {
        return Vec::new();
    };
    text.replace("\r\n", "\n")
        .split("\n\n")
        .filter_map(|block| {
            let mut lines = block.lines();
            let _index = lines.next()?;
            let timing = lines.next()?;
            let (start, end) = timing.split_once("-->")?;
            let text = lines.collect::<Vec<_>>().join(" ");
            if text.trim().is_empty() {
                return None;
            }
            Some(CaptionPreview {
                start_seconds: parse_srt_seconds(start)?,
                end_seconds: parse_srt_seconds(end)?,
                text: text.split_whitespace().collect::<Vec<_>>().join(" "),
            })
        })
        .take(9)
        .collect()
}

fn revision_catalog_path(root: &Path) -> PathBuf {
    root.join(".reelbrain/desktop/revision-drafts.json")
}

fn revision_audit_path(root: &Path) -> PathBuf {
    root.join(".reelbrain/desktop/revision-feedback.jsonl")
}

fn read_revision_catalog(root: &Path) -> Vec<RevisionDraftRecord> {
    let path = revision_catalog_path(root);
    fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str::<Vec<RevisionDraftRecord>>(&text).ok())
        .unwrap_or_default()
}

fn write_revision_catalog(root: &Path, records: &[RevisionDraftRecord]) -> Result<(), String> {
    let path = revision_catalog_path(root);
    let parent = path.parent().ok_or("Revision catalog has no parent")?;
    fs::create_dir_all(parent)
        .map_err(|error| format!("Could not create revision catalog directory: {error}"))?;
    let temporary = parent.join(format!(
        ".revision-drafts.{}.{}.tmp",
        std::process::id(),
        unix_time_ms()
    ));
    let bytes = serde_json::to_vec_pretty(records)
        .map_err(|error| format!("Could not encode revision catalog: {error}"))?;
    fs::write(&temporary, bytes)
        .map_err(|error| format!("Could not write revision catalog: {error}"))?;
    fs::rename(&temporary, &path)
        .map_err(|error| format!("Could not commit revision catalog: {error}"))
}

fn revision_record_output(record: &RevisionDraftRecord) -> ReviewOutput {
    ReviewOutput {
        output_id: record.draft_id.clone(),
        mode: record.mode.clone(),
        title: record.title.clone(),
        duration_seconds: record.duration_seconds,
        video: record.video.clone(),
        thumbnail: image_data_url(&record.thumbnail).unwrap_or_default(),
        source_range: record.source_range,
        rationale: record.rationale.clone(),
        status: record.status.clone(),
        caption_accuracy_status: record.caption_accuracy_status.clone(),
        caption_preview: record
            .captions_en
            .as_deref()
            .map(caption_preview)
            .unwrap_or_default(),
        base_output_id: Some(record.base_output_id.clone()),
        parent_output_id: Some(record.parent_output_id.clone()),
        version: record.version,
        is_revision: true,
        revision_summary: Some(record.summary.clone()),
        feedback_status: record.feedback_status.clone(),
        feedback_reason: record.feedback_reason.clone(),
        feedback_event_id: record.feedback_event_id.clone(),
        created_at: Some(record.created_at.clone()),
    }
}

fn load_review_run() -> ReviewRun {
    let project_title = "The Memory Is Not Evidence".to_owned();
    let Some(manifest_path) = find_latest_manifest(&workspace_root()) else {
        return ReviewRun {
            available: false,
            status: "NO_RUN".into(),
            project_title,
            manifest_path: None,
            outputs: Vec::new(),
        };
    };
    let Ok(text) = fs::read_to_string(&manifest_path) else {
        return ReviewRun {
            available: false,
            status: "UNREADABLE_RUN".into(),
            project_title,
            manifest_path: Some(manifest_path.to_string_lossy().into_owned()),
            outputs: Vec::new(),
        };
    };
    let Ok(document) = serde_json::from_str::<Value>(&text) else {
        return ReviewRun {
            available: false,
            status: "INVALID_RUN".into(),
            project_title,
            manifest_path: Some(manifest_path.to_string_lossy().into_owned()),
            outputs: Vec::new(),
        };
    };
    let outputs = document
        .get("outputs")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|output| {
            let range = output.get("source_range")?.as_array()?;
            let captions_en = output
                .get("captions_en")
                .and_then(Value::as_str)
                .unwrap_or_default();
            Some(ReviewOutput {
                output_id: output.get("output_id")?.as_str()?.to_owned(),
                mode: output.get("mode")?.as_str()?.to_owned(),
                title: output.get("title")?.as_str()?.to_owned(),
                duration_seconds: output.get("duration_seconds")?.as_f64()?,
                video: output.get("video")?.as_str()?.to_owned(),
                thumbnail: output
                    .get("thumbnail")
                    .and_then(Value::as_str)
                    .and_then(image_data_url)
                    .unwrap_or_default(),
                source_range: [range.first()?.as_f64()?, range.get(1)?.as_f64()?],
                rationale: output
                    .get("rationale")
                    .and_then(Value::as_str)
                    .unwrap_or("Grounded creator-review draft")
                    .to_owned(),
                status: output
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or("CREATOR_REVIEW")
                    .to_owned(),
                caption_accuracy_status: output
                    .get("caption_accuracy_status")
                    .and_then(Value::as_str)
                    .unwrap_or("UNVERIFIED_CREATOR_CORRECTION_REQUIRED")
                    .to_owned(),
                caption_preview: caption_preview(captions_en),
                base_output_id: None,
                parent_output_id: None,
                version: 1,
                is_revision: false,
                revision_summary: None,
                feedback_status: "pending".into(),
                feedback_reason: None,
                feedback_event_id: None,
                created_at: None,
            })
        })
        .collect::<Vec<_>>();
    let mut outputs = outputs;
    outputs.extend(
        read_revision_catalog(&workspace_root())
            .iter()
            .filter(|record| Path::new(&record.video).is_file())
            .map(revision_record_output),
    );
    ReviewRun {
        available: true,
        status: document
            .get("status")
            .and_then(Value::as_str)
            .unwrap_or("CREATOR_REVIEW")
            .to_owned(),
        project_title,
        manifest_path: Some(manifest_path.to_string_lossy().into_owned()),
        outputs,
    }
}

fn emit_revision_progress(
    app: &tauri::AppHandle,
    job_id: &str,
    phase: &str,
    progress: u8,
    detail: &str,
    status: &str,
    output_id: Option<String>,
) {
    let _ = app.emit(
        "revision-progress",
        RevisionProgress {
            job_id: job_id.to_owned(),
            phase: phase.to_owned(),
            progress,
            detail: detail.to_owned(),
            status: status.to_owned(),
            output_id,
        },
    );
}

fn extract_revision_render_plan(response: &str) -> Result<RevisionRenderPlan, String> {
    let start = response
        .find(REVISION_PLAN_START)
        .ok_or("Revision planner did not return a render plan")?;
    let payload_start = start + REVISION_PLAN_START.len();
    let relative_end = response[payload_start..]
        .find(REVISION_PLAN_END)
        .ok_or("Revision planner returned an incomplete render plan")?;
    let mut plan: RevisionRenderPlan = serde_json::from_str(
        response[payload_start..payload_start + relative_end].trim(),
    )
    .map_err(|error| format!("Revision planner returned invalid JSON: {error}"))?;
    plan.contrast = plan.contrast.clamp(0.9, 1.15);
    plan.saturation = plan.saturation.clamp(0.85, 1.15);
    plan.sharpen = plan.sharpen.clamp(0.0, 0.6);
    plan.audio_target_lufs = plan.audio_target_lufs.clamp(-20.0, -12.0);
    plan.rationale = validate_agent_profile_text("Render plan rationale", &plan.rationale, 800)?;
    if !plan.supported && plan.unsupported_reason.as_deref().unwrap_or_default().trim().is_empty() {
        return Err("Unsupported revision plan must explain the missing capability".into());
    }
    Ok(plan)
}

fn plan_revision_at(request: PlanRevisionRequest) -> Result<RevisionRenderPlan, String> {
    let instruction = validate_agent_profile_text("Revision instruction", &request.instruction, 12_000)?;
    let request_id = validate_agent_profile_text("Revision request id", &request.request_id, 160)?;
    let prompt = format!(
        "Create a bounded render plan for this already approved ReelBrain revision.\n\nJob: {}\nDraft: {}\nMode: {}\nDuration: {:.2}s\nCurrent rationale: {}\nCreator instruction: {}",
        request_id,
        request.title,
        request.mode,
        request.duration_seconds,
        request.rationale,
        instruction
    );
    let instructions = r#"You are ReelBrain's Style Editor planning parameters for the approved local `render-revision-draft` semantic tool. This is execution planning, not intent routing or approval. Use the creator's natural-language instruction as the source of truth.

The current bounded renderer can change only visual contrast, saturation, sharpening, and audio loudness while preserving the complete timeline and burned captions. It cannot honestly perform cuts, reorder moments, rewrite captions, add images, reframe, or change music.

Return exactly one marker and no other text:
<reelbrain-render-plan>{"supported":true,"contrast":1.0,"saturation":1.0,"sharpen":0.2,"audioTargetLufs":-16.0,"rationale":"how these values apply the request","unsupportedReason":null}</reelbrain-render-plan>

Bounds: contrast 0.90-1.15, saturation 0.85-1.15, sharpen 0.0-0.6, audioTargetLufs -20 to -12. Choose neutral 1.0 values when the creator asks to preserve that dimension. If the requested revision requires capabilities outside this renderer, set supported=false, keep neutral values, and give a concise unsupportedReason. Never claim that rendering occurred."#;
    let result = run_codex_turn(
        prompt,
        &[],
        None,
        workspace_root(),
        instructions,
        "reelbrain-revision-planner",
        Duration::from_secs(90),
        "Style Editor",
        None,
    )?;
    extract_revision_render_plan(&result.response)
}

fn copy_revision_sidecars(source: &Path, destination: &Path) -> Option<String> {
    let mut english = None;
    for extension in ["en.srt", "ko.srt", "ass"] {
        let source_sidecar = source.with_extension(extension);
        if !source_sidecar.is_file() {
            continue;
        }
        let destination_sidecar = destination.with_extension(extension);
        if fs::copy(&source_sidecar, &destination_sidecar).is_ok() && extension == "en.srt" {
            english = Some(destination_sidecar.to_string_lossy().into_owned());
        }
    }
    english
}

fn execute_revision_at(
    app: &tauri::AppHandle,
    root: &Path,
    request: ExecuteRevisionRequest,
) -> Result<ReviewOutput, String> {
    let instruction = request.instruction.trim();
    let summary = request.summary.trim();
    if instruction.is_empty() || summary.is_empty() {
        return Err("A revision instruction and summary are required".into());
    }
    if instruction.len() > 12_000 || summary.len() > 1_000 {
        return Err("The revision request is too large".into());
    }
    if request.job_id.is_empty()
        || !request
            .job_id
            .chars()
            .all(|character| character.is_ascii_alphanumeric() || matches!(character, '-' | '_'))
    {
        return Err("Revision job id is invalid".into());
    }

    emit_revision_progress(
        app,
        &request.job_id,
        "Preparing revision",
        6,
        "Resolving the approved draft and creating an isolated version directory.",
        "running",
        None,
    );
    let run = load_review_run();
    let parent = run
        .outputs
        .iter()
        .find(|output| output.output_id == request.base_output_id)
        .cloned()
        .ok_or("The selected draft no longer exists")?;
    let source = PathBuf::from(&parent.video)
        .canonicalize()
        .map_err(|error| format!("Could not open the selected draft: {error}"))?;
    if !source.is_file() || source.is_symlink() {
        return Err("The selected draft is not a regular local video".into());
    }
    let root_output_id = parent
        .base_output_id
        .clone()
        .unwrap_or_else(|| parent.output_id.clone());
    let mut records = read_revision_catalog(root);
    let version = records
        .iter()
        .filter(|record| record.base_output_id == root_output_id)
        .map(|record| record.version)
        .max()
        .unwrap_or(1)
        + 1;
    let draft_id = format!("{}-v{}", root_output_id, version);
    let directory = root
        .join(".reelbrain/desktop/revisions")
        .join(format!("{}-{}", request.job_id, draft_id));
    if directory.exists() {
        return Err("This revision job already exists".into());
    }
    fs::create_dir_all(&directory)
        .map_err(|error| format!("Could not create the revision workspace: {error}"))?;
    let output = directory.join("draft.mp4");
    let thumbnail = directory.join("thumbnail.jpg");
    let source_digest = digest_file(&source)?;

    emit_revision_progress(
        app,
        &request.job_id,
        "Rendering approved edit",
        16,
        "The ReelBrain assembler is running the bounded local render tool. The original remains untouched.",
        "running",
        None,
    );
    if !request.render_plan.supported {
        return Err(request
            .render_plan
            .unsupported_reason
            .clone()
            .unwrap_or_else(|| "The approved revision needs a capability outside the current renderer".into()));
    }
    let plan = request.render_plan.clone();
    let video_filter = format!(
        "eq=contrast={:.3}:saturation={:.3},unsharp=5:5:{:.3}:5:5:0.0",
        plan.contrast,
        plan.saturation,
        plan.sharpen
    );
    let audio_filter = format!("loudnorm=I={:.1}:TP=-1.5:LRA=11", plan.audio_target_lufs);
    let mut command = Command::new("ffmpeg");
    command
        .args(["-y", "-i"])
        .arg(&source)
        .args(["-map", "0:v:0", "-map", "0:a:0", "-vf"])
        .arg(video_filter)
        .arg("-af")
        .arg(audio_filter)
        .args([
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-movflags",
            "+faststart",
            "-map_metadata",
            "-1",
            "-sn",
            "-dn",
            "-progress",
            "pipe:1",
            "-nostats",
        ])
        .arg(&output)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let mut child = command
        .spawn()
        .map_err(|error| format!("Could not start the local renderer: {error}"))?;
    if let Some(stdout) = child.stdout.take() {
        for line in BufReader::new(stdout).lines().map_while(Result::ok) {
            let Some((key, raw_value)) = line.split_once('=') else {
                continue;
            };
            if !matches!(key, "out_time_ms" | "out_time_us") {
                continue;
            }
            let rendered = raw_value.parse::<f64>().unwrap_or_default() / 1_000_000.0;
            let ratio = if parent.duration_seconds > 0.0 {
                (rendered / parent.duration_seconds).clamp(0.0, 1.0)
            } else {
                0.0
            };
            emit_revision_progress(
                app,
                &request.job_id,
                "Rendering approved edit",
                (16.0 + ratio * 66.0).round() as u8,
                &format!(
                    "Rendered {:.0}s of {:.0}s into a new, non-destructive draft.",
                    rendered.min(parent.duration_seconds),
                    parent.duration_seconds
                ),
                "running",
                None,
            );
        }
    }
    let status = child
        .wait()
        .map_err(|error| format!("Could not wait for the local renderer: {error}"))?;
    if !status.success() {
        emit_revision_progress(
            app,
            &request.job_id,
            "Render failed",
            100,
            "FFmpeg did not produce a valid draft. The current version was left unchanged.",
            "failed",
            None,
        );
        return Err("The local renderer failed; no new draft was registered".into());
    }

    emit_revision_progress(
        app,
        &request.job_id,
        "Verifying new draft",
        88,
        "Checking streams, duration, thumbnail, and content digest before showing the version.",
        "running",
        None,
    );
    let inspected = inspect_video(&output)?;
    if inspected.status != "ready" {
        return Err("Rendered draft failed audio/video verification".into());
    }
    let output_digest = inspected.sha256.clone();
    if output_digest == source_digest {
        return Err("Renderer output is identical to the selected draft".into());
    }
    let duration_seconds = inspected.duration_seconds.unwrap_or(parent.duration_seconds);
    if (duration_seconds - parent.duration_seconds).abs() > 1.0 {
        return Err("Rendered draft duration changed outside the bounded tolerance".into());
    }
    let thumbnail_status = Command::new("ffmpeg")
        .args(["-y", "-ss", "1", "-i"])
        .arg(&output)
        .args(["-frames:v", "1", "-q:v", "2"])
        .arg(&thumbnail)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map_err(|error| format!("Could not generate the revision thumbnail: {error}"))?;
    if !thumbnail_status.success() || !thumbnail.is_file() {
        return Err("Rendered draft thumbnail verification failed".into());
    }
    let captions_en = copy_revision_sidecars(&source, &output);
    let record = RevisionDraftRecord {
        draft_id: draft_id.clone(),
        base_output_id: root_output_id,
        parent_output_id: parent.output_id.clone(),
        version,
        mode: parent.mode.clone(),
        title: format!("{} · v{}", parent.title.trim_end_matches(&format!(" · v{}", parent.version)), version),
        duration_seconds,
        video: output.to_string_lossy().into_owned(),
        thumbnail: thumbnail.to_string_lossy().into_owned(),
        captions_en,
        source_range: parent.source_range,
        rationale: format!("Revision v{version}: {summary}"),
        caption_accuracy_status: parent.caption_accuracy_status.clone(),
        instruction: instruction.to_owned(),
        summary: summary.to_owned(),
        status: "CREATOR_REVIEW".into(),
        feedback_status: "pending".into(),
        feedback_reason: None,
        feedback_event_id: None,
        created_at: unix_time_ms().to_string(),
        source_digest,
        output_digest,
    };
    records.push(record.clone());
    write_revision_catalog(root, &records)?;
    let evidence = json!({
        "schema": "reelbrain.dev/revision-evidence/v1",
        "draft_id": record.draft_id,
        "base_output_id": record.base_output_id,
        "parent_output_id": record.parent_output_id,
        "version": record.version,
        "instruction": record.instruction,
        "summary": record.summary,
        "render_plan": plan,
        "source_digest": record.source_digest,
        "output_digest": record.output_digest,
        "renderer": "render-revision-draft",
        "creator_review_required": true,
        "publish_ready": false
    });
    fs::write(
        directory.join("evidence.json"),
        serde_json::to_vec_pretty(&evidence).map_err(|error| error.to_string())?,
    )
    .map_err(|error| format!("Could not write revision evidence: {error}"))?;
    emit_revision_progress(
        app,
        &request.job_id,
        "New draft ready",
        100,
        "The changed video passed local verification and is ready for your Like or Dislike decision.",
        "completed",
        Some(draft_id),
    );
    Ok(revision_record_output(&record))
}

fn record_revision_feedback_at(
    root: &Path,
    request: RevisionFeedbackRequest,
) -> Result<Value, String> {
    if !matches!(request.decision.as_str(), "like" | "dislike" | "skip") {
        return Err("Revision feedback must be Like, Dislike, or Skip".into());
    }
    let reason = request.reason.as_deref().unwrap_or_default().trim();
    if request.decision == "dislike" && reason.is_empty() {
        return Err("A reason is required before ReelBrain can make the next draft".into());
    }
    if request.creator_statement.trim().is_empty() {
        return Err("An explicit creator feedback statement is required".into());
    }
    let mut records = read_revision_catalog(root);
    let record = records
        .iter_mut()
        .find(|record| record.draft_id == request.output_id)
        .ok_or("Revision draft was not found")?;
    if record.feedback_status != "pending" {
        return Err("This draft already has creator feedback".into());
    }
    let event_id = format!(
        "revision_feedback_{}",
        &format!(
            "{:x}",
            Sha256::digest(
                format!(
                    "{}:{}:{}:{}",
                    request.output_id,
                    request.decision,
                    reason,
                    unix_time_ms()
                )
                .as_bytes()
            )
        )[..24]
    );
    record.feedback_status = match request.decision.as_str() {
        "like" => "liked",
        "dislike" => "disliked",
        _ => "skipped",
    }
    .into();
    record.feedback_reason = (!reason.is_empty()).then(|| reason.to_owned());
    record.feedback_event_id = Some(event_id.clone());
    let output = revision_record_output(record);
    write_revision_catalog(root, &records)?;
    let audit_path = revision_audit_path(root);
    if let Some(parent) = audit_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("Could not create revision audit directory: {error}"))?;
    }
    let event = json!({
        "event_id": event_id,
        "event_type": "creator_revision_feedback",
        "creator_id": "creator-founder",
        "project_id": "founder-desktop-project",
        "output_id": request.output_id,
        "decision": request.decision,
        "reason": if reason.is_empty() { Value::Null } else { Value::String(reason.to_owned()) },
        "creator_statement": request.creator_statement,
        "publish_ready": false,
        "at_ms": unix_time_ms()
    });
    let mut handle = OpenOptions::new()
        .create(true)
        .append(true)
        .open(audit_path)
        .map_err(|error| format!("Could not open revision feedback audit: {error}"))?;
    serde_json::to_writer(&mut handle, &event)
        .map_err(|error| format!("Could not write revision feedback audit: {error}"))?;
    handle
        .write_all(b"\n")
        .map_err(|error| format!("Could not finish revision feedback audit: {error}"))?;
    handle
        .sync_all()
        .map_err(|error| format!("Could not sync revision feedback audit: {error}"))?;
    Ok(json!({ "output": output, "eventId": event_id }))
}

#[tauri::command]
async fn codex_status() -> ConnectionStatus {
    tauri::async_runtime::spawn_blocking(account_status)
        .await
        .ok()
        .and_then(Result::ok)
        .unwrap_or_else(|| ConnectionStatus {
            connected: false,
            auth_mode: None,
            email: None,
            plan_type: None,
            requires_openai_auth: true,
            detail: "Codex is unavailable. Install Codex or set REELBRAIN_CODEX_BIN.".into(),
        })
}

#[tauri::command]
fn codex_login() -> Result<(), String> {
    Command::new(resolve_codex_bin())
        .arg("login")
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(|_| ())
        .map_err(|error| format!("Could not start the official Codex login flow: {error}"))
}

#[tauri::command]
async fn codex_chat(app: tauri::AppHandle, request: ChatRequest) -> Result<ChatResult, String> {
    let sink = activity_sink(app, request.request_id.clone());
    tauri::async_runtime::spawn_blocking(move || run_chat(request, Some(sink)))
        .await
        .map_err(|error| format!("Codex chat task failed: {error}"))?
}

#[tauri::command]
fn decide_tool_approval(
    request: ToolApprovalDecisionRequest,
) -> Result<ToolApprovalRequest, String> {
    decide_tool_approval_at(&workspace_root(), request)
}

#[tauri::command]
async fn build_and_test_tool(request: ToolBuildRequest) -> Result<ToolApprovalRequest, String> {
    tauri::async_runtime::spawn_blocking(move || {
        build_and_test_tool_at(&workspace_root(), &request.approval_id)
    })
    .await
    .map_err(|error| format!("Tool build task failed: {error}"))?
}

#[tauri::command]
fn deploy_tested_tool(
    request: ToolDeploymentDecisionRequest,
) -> Result<ToolApprovalRequest, String> {
    deploy_tested_tool_at(&workspace_root(), request)
}

#[tauri::command]
fn read_agent_profiles() -> Result<AgentProfileState, String> {
    load_agent_profiles()
}

#[tauri::command]
fn update_agent_profile(request: UpdateAgentProfileRequest) -> Result<AgentProfileState, String> {
    update_agent_profile_at(&workspace_root(), request)
}

#[tauri::command]
async fn codex_persona_chat(
    app: tauri::AppHandle,
    request: PersonaChatRequest,
) -> Result<ChatResult, String> {
    let sink = activity_sink(app, request.request_id.clone());
    tauri::async_runtime::spawn_blocking(move || run_persona_chat(request, Some(sink)))
        .await
        .map_err(|error| format!("Codex persona chat task failed: {error}"))?
}

#[tauri::command]
async fn codex_team_chat(
    app: tauri::AppHandle,
    request: TeamChatRequest,
) -> Result<TeamChatResult, String> {
    let sink = activity_sink(app, request.request_id.clone());
    tauri::async_runtime::spawn_blocking(move || run_team_chat(request, Some(sink)))
        .await
        .map_err(|error| format!("Codex team chat task failed: {error}"))?
}

#[tauri::command]
async fn start_editorial_fanout(
    app: tauri::AppHandle,
    request: FanoutStartRequest,
) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || run_editorial_fanout(app, request))
        .await
        .map_err(|error| format!("Editorial fan-out task failed: {error}"))?
}

#[tauri::command]
async fn inspect_creator_memory() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| {
        call_reelbrain_bridge(
            "memory_inspect",
            json!({"creator_id": "creator-founder"}),
            Duration::from_secs(10),
        )
    })
    .await
    .map_err(|error| format!("Memory inspection task failed: {error}"))?
}

#[tauri::command]
async fn mutate_creator_memory(request: Value) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        call_reelbrain_bridge("memory_mutate", request, Duration::from_secs(10))
    })
    .await
    .map_err(|error| format!("Memory mutation task failed: {error}"))?
}

#[tauri::command]
async fn inspect_fanout_evidence() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(|| {
        let mut evidence = call_reelbrain_bridge(
            "fanout_evidence",
            json!({"limit": 150}),
            Duration::from_secs(10),
        )?;
        let audit_path = revision_audit_path(&workspace_root());
        if let Ok(text) = fs::read_to_string(audit_path) {
            let events = evidence
                .as_object_mut()
                .and_then(|document| document.get_mut("events"))
                .and_then(Value::as_array_mut);
            if let Some(events) = events {
                for raw in text.lines().filter(|line| !line.trim().is_empty()) {
                    let Ok(event) = serde_json::from_str::<Value>(raw) else { continue };
                    let decision = event.get("decision").and_then(Value::as_str).unwrap_or("feedback");
                    events.push(json!({
                        "event_id": event.get("event_id").cloned().unwrap_or(Value::Null),
                        "event_type": "creator_revision_feedback",
                        "actor": "creator-founder",
                        "decision": "allow",
                        "reason_code": format!("revision_{decision}"),
                        "receipt_id": event.get("event_id").cloned().unwrap_or(Value::Null),
                        "created_at": event.get("at_ms").and_then(Value::as_u64).map(|value| value.to_string()),
                        "details": {
                            "output_id": event.get("output_id").cloned().unwrap_or(Value::Null),
                            "feedback": decision,
                            "reason": event.get("reason").cloned().unwrap_or(Value::Null),
                            "publish_ready": false
                        }
                    }));
                }
            }
        }
        Ok(evidence)
    })
    .await
    .map_err(|error| format!("Evidence inspection task failed: {error}"))?
}

#[tauri::command]
async fn steer_editorial_fanout(request: Value) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        call_reelbrain_bridge("fanout_steer", request, Duration::from_secs(10))
    })
    .await
    .map_err(|error| format!("Fan-out steering task failed: {error}"))?
}

#[tauri::command]
async fn record_review_action(request: Value) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        call_reelbrain_bridge("review_action", request, Duration::from_secs(10))
    })
    .await
    .map_err(|error| format!("Creator review task failed: {error}"))?
}

#[tauri::command]
async fn execute_revision(
    app: tauri::AppHandle,
    request: ExecuteRevisionRequest,
) -> Result<ReviewOutput, String> {
    let root = workspace_root();
    let event_app = app.clone();
    let job_id = request.job_id.clone();
    tauri::async_runtime::spawn_blocking(move || execute_revision_at(&app, &root, request))
        .await
        .map_err(|error| format!("Revision render task failed: {error}"))?
        .map_err(|error| {
            emit_revision_progress(
                &event_app,
                &job_id,
                "Revision failed",
                100,
                &error,
                "failed",
                None,
            );
            error
        })
}

#[tauri::command]
async fn plan_revision(request: PlanRevisionRequest) -> Result<RevisionRenderPlan, String> {
    tauri::async_runtime::spawn_blocking(move || plan_revision_at(request))
        .await
        .map_err(|error| format!("Revision planning task failed: {error}"))?
}

#[tauri::command]
async fn record_revision_feedback(request: RevisionFeedbackRequest) -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(move || {
        record_revision_feedback_at(&workspace_root(), request)
    })
    .await
    .map_err(|error| format!("Revision feedback task failed: {error}"))?
}

fn command_available(command: &str, version_arg: &str) -> bool {
    Command::new(command)
        .arg(version_arg)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

#[tauri::command]
fn runtime_health() -> Value {
    json!({
        "codex": resolve_codex_bin().is_file() || command_available("codex", "--version"),
        "python": resolve_python_bin().is_file() || command_available("python3", "--version"),
        "ffmpeg": command_available("ffmpeg", "-version"),
        "ffprobe": command_available("ffprobe", "-version"),
        "workspace": workspace_root(),
        "chatTimeoutSeconds": 90,
        "agentTimeoutSeconds": 120,
        "bridgeTimeoutSeconds": 20,
    })
}

#[tauri::command]
async fn preflight_video(path: String) -> Result<VideoPreflight, String> {
    tauri::async_runtime::spawn_blocking(move || inspect_video(Path::new(&path)))
        .await
        .map_err(|error| format!("Video preflight task failed: {error}"))?
}

#[tauri::command]
fn discover_review_run() -> ReviewRun {
    load_review_run()
}

#[tauri::command]
fn prepare_media_preview(
    path: String,
    media_server: tauri::State<'_, MediaServer>,
) -> Result<String, String> {
    media_server.register(Path::new(&path))
}

#[tauri::command]
fn persist_chat_image(request: PersistChatImageRequest) -> Result<String, String> {
    persist_chat_image_at(&workspace_root(), request)
}

#[tauri::command]
fn prepare_chat_image(path: String) -> Result<String, String> {
    prepare_chat_image_at(Path::new(&path))
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let media_server = MediaServer::start().expect("unable to start local media preview server");
    tauri::Builder::default()
        .manage(media_server)
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            codex_status,
            codex_login,
            codex_chat,
            decide_tool_approval,
            build_and_test_tool,
            deploy_tested_tool,
            read_agent_profiles,
            update_agent_profile,
            codex_persona_chat,
            codex_team_chat,
            start_editorial_fanout,
            inspect_creator_memory,
            mutate_creator_memory,
            inspect_fanout_evidence,
            steer_editorial_fanout,
            record_review_action,
            execute_revision,
            plan_revision,
            record_revision_feedback,
            runtime_health,
            preflight_video,
            discover_review_run,
            prepare_media_preview,
            persist_chat_image,
            prepare_chat_image
        ])
        .run(tauri::generate_context!())
        .expect("error while running ReelBrain Desktop");
}

#[cfg(test)]
mod tests {
    use super::*;

    fn unique_test_root(label: &str) -> PathBuf {
        let nonce = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let root =
            env::temp_dir().join(format!("reelbrain-{label}-{}-{nonce}", std::process::id()));
        fs::create_dir_all(&root).expect("temporary ReelBrain root");
        root
    }

    #[test]
    fn persists_exactly_four_creator_configured_agents() {
        let root = unique_test_root("agent-profiles");
        let initial = load_agent_profiles_at(&root).expect("default profiles");
        assert_eq!(initial.revision, 1);
        assert_eq!(initial.profiles.len(), 4);

        let updated = update_agent_profile_at(
            &root,
            UpdateAgentProfileRequest {
                expected_revision: initial.revision,
                id: "meaning-scout".into(),
                name: "Narrative Architect".into(),
                role: "Find the smallest complete educational story.".into(),
                system_prompt: "Prefer a question, explanation, and grounded payoff.".into(),
            },
        )
        .expect("updated profile");
        assert_eq!(updated.revision, 2);

        let reloaded = load_agent_profiles_at(&root).expect("persisted profiles");
        assert_eq!(reloaded.profiles.len(), 4);
        let profile = reloaded
            .profiles
            .iter()
            .find(|profile| profile.id == "meaning-scout")
            .expect("configured story agent");
        assert_eq!(profile.name, "Narrative Architect");
        assert_eq!(
            profile.role,
            "Find the smallest complete educational story."
        );
        assert!(agent_profiles_path(&root).is_file());

        fs::remove_dir_all(root).expect("remove temporary ReelBrain root");
    }

    #[test]
    fn profile_updates_are_revision_gated_and_mentions_are_unambiguous() {
        let root = unique_test_root("agent-profile-guards");
        let initial = load_agent_profiles_at(&root).expect("default profiles");
        let stale = update_agent_profile_at(
            &root,
            UpdateAgentProfileRequest {
                expected_revision: initial.revision + 1,
                id: "hook-scout".into(),
                name: "한국어 리텐션 편집자".into(),
                role: "한국어 후킹과 리듬을 검토한다.".into(),
                system_prompt: "근거를 보존하면서 첫 문장을 강화한다.".into(),
            },
        )
        .expect_err("stale revision must fail");
        assert!(stale.contains("reload before saving"));

        let duplicate = update_agent_profile_at(
            &root,
            UpdateAgentProfileRequest {
                expected_revision: initial.revision,
                id: "hook-scout".into(),
                name: "Story Editor".into(),
                role: "Duplicate mention".into(),
                system_prompt: "This must not be stored.".into(),
            },
        )
        .expect_err("duplicate mention name must fail");
        assert!(duplicate.contains("unique @mention"));
        assert_eq!(
            agent_mention_key("한국어 리텐션 편집자"),
            "한국어-리텐션-편집자"
        );

        fs::remove_dir_all(root).expect("remove temporary ReelBrain root");
    }

    #[test]
    fn discovers_creator_review_outputs() {
        let run = load_review_run();
        assert!(run.available);
        assert_eq!(run.status, "CREATOR_REVIEW");
        assert_eq!(
            run.outputs
                .iter()
                .filter(|output| !output.is_revision)
                .filter(|output| output.mode == "short")
                .count(),
            12
        );
        assert_eq!(
            run.outputs
                .iter()
                .filter(|output| !output.is_revision)
                .filter(|output| output.mode == "long")
                .count(),
            4
        );
        assert!(run
            .outputs
            .iter()
            .filter(|output| !output.is_revision)
            .all(|output| output.version == 1 && output.feedback_status == "pending"));
    }

    #[test]
    fn revision_feedback_is_single_use_and_persists_review_queue_state() {
        let root = unique_test_root("revision-feedback");
        let record = RevisionDraftRecord {
            draft_id: "source-01-short-01-v2".into(),
            base_output_id: "source-01-short-01".into(),
            parent_output_id: "source-01-short-01".into(),
            version: 2,
            mode: "short".into(),
            title: "A grounded hook · v2".into(),
            duration_seconds: 45.0,
            video: root.join("draft.mp4").to_string_lossy().into_owned(),
            thumbnail: root.join("thumbnail.jpg").to_string_lossy().into_owned(),
            captions_en: None,
            source_range: [0.0, 45.0],
            rationale: "Apply the approved hook correction".into(),
            caption_accuracy_status: "CREATOR_VERIFIED".into(),
            instruction: "Open on the payoff".into(),
            summary: "Open on the payoff".into(),
            status: "CREATOR_REVIEW".into(),
            feedback_status: "pending".into(),
            feedback_reason: None,
            feedback_event_id: None,
            created_at: unix_time_ms().to_string(),
            source_digest: "a".repeat(64),
            output_digest: "b".repeat(64),
        };
        let skipped_record = RevisionDraftRecord {
            draft_id: "source-01-short-01-v3".into(),
            parent_output_id: "source-01-short-01-v2".into(),
            version: 3,
            title: "A grounded hook · v3".into(),
            ..record.clone()
        };
        write_revision_catalog(&root, &[record, skipped_record]).expect("revision catalog");
        let missing_reason = record_revision_feedback_at(
            &root,
            RevisionFeedbackRequest {
                output_id: "source-01-short-01-v2".into(),
                decision: "dislike".into(),
                reason: None,
                creator_statement: "I dislike this draft.".into(),
            },
        )
        .expect_err("dislike must be explained");
        assert!(missing_reason.contains("reason is required"));

        let result = record_revision_feedback_at(
            &root,
            RevisionFeedbackRequest {
                output_id: "source-01-short-01-v2".into(),
                decision: "dislike".into(),
                reason: Some("The hook is too slow; begin on the conclusion.".into()),
                creator_statement: "Use this correction for the next draft.".into(),
            },
        )
        .expect("feedback recorded");
        assert_eq!(
            result["output"]["feedbackStatus"],
            Value::String("disliked".into())
        );
        assert!(revision_audit_path(&root).is_file());
        assert_eq!(read_revision_catalog(&root)[0].feedback_status, "disliked");

        let duplicate = record_revision_feedback_at(
            &root,
            RevisionFeedbackRequest {
                output_id: "source-01-short-01-v2".into(),
                decision: "like".into(),
                reason: None,
                creator_statement: "Change my answer.".into(),
            },
        )
        .expect_err("feedback is single-use");
        assert!(duplicate.contains("already has creator feedback"));

        let skipped = record_revision_feedback_at(
            &root,
            RevisionFeedbackRequest {
                output_id: "source-01-short-01-v3".into(),
                decision: "skip".into(),
                reason: None,
                creator_statement: "Skip this taste decision.".into(),
            },
        )
        .expect("skip recorded without taste reason");
        assert_eq!(
            skipped["output"]["feedbackStatus"],
            Value::String("skipped".into())
        );
        fs::remove_dir_all(root).expect("remove temporary ReelBrain root");
    }

    #[test]
    fn preflight_is_local_and_detects_audio_video() {
        let run = load_review_run();
        let output = run.outputs.first().expect("dogfood output");
        let result = inspect_video(Path::new(&output.video)).expect("video preflight");
        assert_eq!(result.status, "ready");
        assert!(result.has_video);
        assert!(result.has_audio);
        assert!(!result.sha256.is_empty());
    }

    #[test]
    fn parses_browser_media_ranges() {
        assert_eq!(parse_byte_range(None, 100), Ok(None));
        assert_eq!(parse_byte_range(Some("bytes=0-9"), 100), Ok(Some((0, 9))));
        assert_eq!(parse_byte_range(Some("bytes=90-"), 100), Ok(Some((90, 99))));
        assert_eq!(parse_byte_range(Some("bytes=-10"), 100), Ok(Some((90, 99))));
        assert_eq!(parse_byte_range(Some("bytes=100-"), 100), Err(()));
    }

    #[test]
    fn maps_codex_tool_items_to_safe_chat_activity() {
        let started = item_activity(
            &json!({
                "type": "mcpToolCall",
                "id": "tool-1",
                "server": "reelbrain",
                "tool": "inspect_creator_memory",
                "status": "inProgress",
                "arguments": {"creator_id": "creator-founder", "include_disabled": false}
            }),
            "Style Editor",
            "started",
        )
        .expect("MCP activity");
        assert_eq!(started.kind, "tool");
        assert_eq!(started.status, "running");
        assert_eq!(started.title, "Call inspect_creator_memory");
        assert!(started
            .detail
            .as_deref()
            .unwrap_or_default()
            .contains("creator_id"));
        assert!(!started
            .detail
            .as_deref()
            .unwrap_or_default()
            .contains("creator-founder"));

        let completed = item_activity(
            &json!({
                "type": "mcpToolCall",
                "id": "tool-1",
                "server": "reelbrain",
                "tool": "inspect_creator_memory",
                "status": "completed",
                "arguments": {}
            }),
            "Style Editor",
            "completed",
        )
        .expect("completed MCP activity");
        assert_eq!(completed.status, "completed");
    }

    #[test]
    fn redacts_sensitive_command_activity_values() {
        let detail = command_activity_detail(
            "curl --api-key super-secret-value --token=another-secret https://example.test",
        );
        assert!(!detail.contains("super-secret-value"));
        assert!(!detail.contains("another-secret"));
        assert!(detail.contains("••••"));
    }

    #[test]
    fn persists_pasted_chat_images_by_content_digest() {
        let root = unique_test_root("chat-image");
        let bytes = vec![137, 80, 78, 71, 13, 10, 26, 10, 0, 0, 0, 0];
        let first = persist_chat_image_at(
            &root,
            PersistChatImageRequest {
                name: "clipboard.png".into(),
                mime_type: "image/png".into(),
                bytes: bytes.clone(),
            },
        )
        .expect("persist pasted image");
        let second = persist_chat_image_at(
            &root,
            PersistChatImageRequest {
                name: "another-name.png".into(),
                mime_type: "image/png".into(),
                bytes,
            },
        )
        .expect("deduplicate pasted image");
        assert_eq!(first, second);
        assert!(Path::new(&first).is_file());
        let preview =
            prepare_chat_image_at(Path::new(&first)).expect("prepare pasted image preview");
        assert!(preview.starts_with("data:image/png;base64,"));
        fs::remove_dir_all(root).expect("remove temporary ReelBrain root");
    }

    #[test]
    fn default_overlay_capability_does_not_request_a_new_tool() {
        let root = unique_test_root("default-overlay-tool");
        let response = format!(
            "Use the supplied image from 10:55 to 11:10.{TOOL_REQUEST_START}{{\"toolName\":\"custom-image-overlay\",\"purpose\":\"Overlay one image\",\"reasonMissing\":\"No overlay tool\",\"capabilities\":[\"media:overlay-image\"],\"dependencies\":[\"ffmpeg\"],\"permissions\":[\"read creator image\"],\"dataEffects\":[\"write revised video\"]}}{TOOL_REQUEST_END}"
        );

        let (clean, approval) =
            extract_tool_approval_request_at(&root, &response, "ReelBrain Showrunner")
                .expect("process tool request");

        assert!(approval.is_none());
        assert!(clean.contains("overlay-timed-image"));
        assert!(!tool_approval_directory(&root).exists());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn missing_tool_waits_for_human_build_approval_and_records_receipt() {
        let root = unique_test_root("missing-tool-approval");
        let response = format!(
            "I need a new bounded capability.{TOOL_REQUEST_START}{{\"toolName\":\"depth-map-parallax\",\"purpose\":\"Create a subtle parallax move from a still image\",\"reasonMissing\":\"No approved tool produces a depth-aware camera move\",\"capabilities\":[\"image:depth-parallax\"],\"dependencies\":[\"approved-depth-model\"],\"permissions\":[\"read creator image\",\"write derived image frames\"],\"dataEffects\":[\"write quarantined tool artifact\"]}}{TOOL_REQUEST_END}"
        );
        let (clean, approval) =
            extract_tool_approval_request_at(&root, &response, "ReelBrain Showrunner")
                .expect("create pending approval");
        let pending = approval.expect("pending approval request");

        assert_eq!(clean, "I need a new bounded capability.");
        assert_eq!(pending.status, "pending_creator_approval");
        assert!(pending.approval_receipt_id.is_none());
        let approved =
            decide_tool_approval_at(
                &root,
                ToolApprovalDecisionRequest {
                    approval_id: pending.approval_id.clone(),
                    decision: "approve".into(),
                    creator_statement:
                        "Approve a quarantined build and audit only; deployment remains blocked."
                            .into(),
                },
            )
            .expect("approve quarantined build");

        assert_eq!(approved.status, "approved_for_quarantined_build");
        assert!(approved
            .approval_receipt_id
            .as_deref()
            .unwrap_or_default()
            .starts_with("approval_"));
        assert!(tool_approval_directory(&root).join("audit.jsonl").is_file());
        fs::remove_dir_all(root).expect("remove temporary ReelBrain root");
    }

    #[test]
    fn tool_build_and_deployment_are_separate_enforced_gates() {
        let root = unique_test_root("tool-two-gate-lifecycle");
        let response = format!(
            "A bounded tool is required.{TOOL_REQUEST_START}{{\"toolName\":\"caption-safe-area\",\"purpose\":\"Calculate readable caption safe areas\",\"reasonMissing\":\"No approved tool calculates this layout\",\"capabilities\":[\"caption:safe-area\"],\"dependencies\":[],\"permissions\":[],\"dataEffects\":[\"write quarantined tool artifact\"]}}{TOOL_REQUEST_END}"
        );
        let (_, approval) =
            extract_tool_approval_request_at(&root, &response, "ReelBrain Showrunner")
                .expect("create approval request");
        let pending = approval.expect("pending request");

        assert!(build_and_test_tool_at(&root, &pending.approval_id)
            .expect_err("unapproved build must fail")
            .contains("not approved"));
        assert!(deploy_tested_tool_at(
            &root,
            ToolDeploymentDecisionRequest {
                approval_id: pending.approval_id.clone(),
                decision: "approve".into(),
                creator_statement: "Deploy this tool.".into(),
            },
        )
        .expect_err("untested deployment must fail")
        .contains("has not passed"));

        decide_tool_approval_at(
            &root,
            ToolApprovalDecisionRequest {
                approval_id: pending.approval_id.clone(),
                decision: "approve".into(),
                creator_statement: "Approve only a quarantined build and independent tests.".into(),
            },
        )
        .expect("approve build and test");

        fs::create_dir_all(tool_build_directory(&root, &pending.approval_id))
            .expect("simulate unsafe pre-existing build directory");
        let failed = build_and_test_tool_at(&root, &pending.approval_id)
            .expect("failed audit is persisted as a blocked result");
        assert_eq!(failed.status, "build_or_test_failed");
        assert_eq!(failed.test_status.as_deref(), Some("failed"));
        assert!(failed
            .test_summary
            .as_deref()
            .unwrap_or_default()
            .contains("build directory already exists"));
        assert!(deploy_tested_tool_at(
            &root,
            ToolDeploymentDecisionRequest {
                approval_id: pending.approval_id,
                decision: "approve".into(),
                creator_statement: "Deploy despite the failed test.".into(),
            },
        )
        .expect_err("failed tool must remain blocked")
        .contains("has not passed"));

        fs::remove_dir_all(root).expect("remove temporary ReelBrain root");
    }

    #[test]
    fn revision_approval_is_requested_by_llm_marker_not_creator_text_matching() {
        let response = format!(
            "I recommend tightening the caption rhythm. {REVISION_PROPOSAL_START}{{\"summary\":\"Retiming the selected captions without changing meaning\"}}{REVISION_PROPOSAL_END}"
        );
        let (clean, proposal) = extract_revision_proposal(&response).expect("revision marker");
        assert_eq!(clean, "I recommend tightening the caption rhythm.");
        assert_eq!(
            proposal.expect("proposal").summary,
            "Retiming the selected captions without changing meaning"
        );
        assert!(extract_revision_proposal("좋아 그렇게 진행하자")
            .expect("ordinary creator text")
            .1
            .is_none());
    }

    #[test]
    fn revision_render_plan_is_model_structured_and_bounded() {
        let response = format!(
            "{REVISION_PLAN_START}{{\"supported\":true,\"contrast\":0.72,\"saturation\":1.4,\"sharpen\":0.25,\"audioTargetLufs\":-16,\"rationale\":\"Use gentler contrast while preserving the timeline.\",\"unsupportedReason\":null}}{REVISION_PLAN_END}"
        );
        let plan = extract_revision_render_plan(&response).expect("bounded render plan");
        assert_eq!(plan.contrast, 0.9);
        assert_eq!(plan.saturation, 1.15);
        assert_eq!(plan.sharpen, 0.25);
        assert!(plan.supported);

        let unsupported = format!(
            "{REVISION_PLAN_START}{{\"supported\":false,\"contrast\":1,\"saturation\":1,\"sharpen\":0,\"audioTargetLufs\":-16,\"rationale\":\"Requires a timeline edit.\",\"unsupportedReason\":null}}{REVISION_PLAN_END}"
        );
        assert!(extract_revision_render_plan(&unsupported).is_err());
    }

    #[test]
    #[ignore = "uses the creator's active Codex account"]
    fn live_revision_render_plan_uses_creator_direction() {
        let plan = plan_revision_at(PlanRevisionRequest {
            instruction: "Use gentler contrast and a more neutral, restrained finish while preserving timing and captions.".into(),
            mode: "short".into(),
            duration_seconds: 55.0,
            title: "QA draft".into(),
            rationale: "Creator-review draft".into(),
            request_id: "live-render-plan-test".into(),
        })
        .expect("live revision plan");
        assert!(plan.supported);
        assert!(plan.contrast <= 1.0);
        assert!(plan.saturation <= 1.05);
    }

    #[test]
    #[ignore = "uses the creator's active Codex account"]
    fn live_codex_chat_round_trip() {
        let result = run_chat(
            ChatRequest {
                prompt: "Reply with exactly: ReelBrain connected.".into(),
                request_id: "live-test".into(),
                image_paths: Vec::new(),
                context: None,
                thread_id: None,
                cwd: Some(workspace_root().to_string_lossy().into_owned()),
            },
            None,
        )
        .expect("Codex chat response");
        assert!(!result.thread_id.is_empty());
        assert!(result.response.contains("ReelBrain connected"));
    }
}
