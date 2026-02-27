//! CI Automation Script
//!
//! This script automates the process of matching local code state to GitLab CI pipelines.
//! It handles triggering new pipelines, attaching to existing ones, monitoring progress,
//! and managing artifacts.
//!
//! # Features
//!
//! ## 1. Pipeline Resolution
//! The script determines the correct pipeline to monitor based on the local git state:
//!
//! ### Clean State (No Uncommitted Changes)
//! - Checks if the local `HEAD` matches the remote `HEAD`.
//! - **Synced**: Finds an existing pipeline for the commit or triggers a new one.
//! - **Mismatch / No Remote**: Prompts the user with options:
//!   0. **Push to Temp Branch (Default)**: Pushes `HEAD` to a temporary branch (`ci-temp-<branch>-<uuid>`) and runs the pipeline there.
//!   1. **Push to Current Branch**: Pushes `HEAD` to `origin/<branch>` and runs the pipeline.
//!   2. **Do Nothing**: Runs the pipeline on the current commit *as known by the remote* (may be outdated).
//!
//! ### Dirty State (Uncommitted Changes)
//! - Calculates a unique hash based on the base commit + diff content (`base_sha` + `diff_md5`).
//! - Searches for any existing pipelines tagged with this metadata (in the commit message).
//! - **Found**: Attaches to the existing pipeline.
//! - **Not Found**:
//!     1.  Creates a temporary local directory.
//!     2.  Copies project files (excluding target/.git).
//!     3.  Initializes a temporary git repo.
//!     4.  Commits changes with the metadata in the message.
//!     5.  Pushes to a temporary branch on origin.
//!     6.  Triggers a pipeline.
//!
//! ## 2. Automatic Cleanup
//! - Temporary branches created (either from Option 0 in Clean state or the Dirty state workflow) are tracked.
//! - When the script exits (success, failure, or Ctrl-C), it attempts to delete these temporary remote branches.
//!
//! ## 3. Pipeline Monitoring
//! - Polls the GitLab API for pipeline status.
//! - Displays a spinner and timer.
//! - streams logs from running jobs to the console.
//!
//! ## 4. Failure & Artifact Handling
//! - On pipeline failure, it prints the last lines of the log for failed jobs.
//! - Scans failed jobs for artifacts (specifically golden image updates).
//! - Prompts the user to automatically download and apply these artifacts to the local codebase.

use anyhow::{Context, Result, bail};
use std::collections::HashMap;
use std::io::Write;
use std::path::Path;
use std::process::Command;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::sleep;
use std::time::{Duration, Instant};
use tempfile::TempDir;
use time::OffsetDateTime;
use time::format_description::well_known::Rfc3339;
use uuid::Uuid;

// [Trait and Real Provider Implementation - Placed at the end of file or module, but for now defining interfaces inline to use in run]

use crate::shell::Shell;

pub trait CiProvider {
    fn current_dir(&self) -> Result<std::path::PathBuf>;
    fn get_gitlab_token(&self) -> Result<String>;
    fn get_project_info(&self) -> Result<(String, String)>; // host, project_path
    fn is_git_clean(&self, dir: &Path) -> Result<bool>;
    fn get_git_head(&self, dir: &Path) -> Result<String>;
    fn get_git_branch(&self, dir: &Path) -> Result<String>;
    fn check_remote_branch_sha(&self, dir: &Path, branch: &str) -> Result<String>;
    fn prompt_user(&self, message: &str, options: &[String], default_idx: usize) -> Result<usize>;
    fn git_push(&self, dir: &Path, branch: &str) -> Result<()>;
    fn git_push_temp(&self, dir: &Path, local_branch: &str, temp_branch_name: &str) -> Result<()>;
    fn is_canceled(&self) -> bool;

    // API wrappers
    fn search_pipeline(
        &self,
        host: &str,
        project_encoded: &str,
        sha: &str,
        branch: Option<&str>,
        diff_md5: Option<&str>,
        token: &str,
    ) -> Result<Option<PipelineInfo>>;
    fn trigger_pipeline(
        &self,
        host: &str,
        project: &str,
        branch: &str,
        token: &str,
    ) -> Result<(u64, String, OffsetDateTime)>;

    // Complex workflow needing abstraction
    fn setup_workflow(
        &self,
        original_dir: &Path,
        base_sha: &str,
        diff_md5: &str,
    ) -> Result<Workflow>;

    // Monitoring
    fn poll_logs(
        &self,
        host: &str,
        project_encoded: &str,
        pipeline_id: u64,
        token: &str,
        cursors: &mut HashMap<u64, usize>,
    ) -> Result<()>;
    fn check_pipeline_status(
        &self,
        host: &str,
        project_encoded: &str,
        pipeline_id: u64,
        token: &str,
    ) -> Result<String>;
    fn handle_failure(
        &self,
        host: &str,
        project_encoded: &str,
        pipeline_id: u64,
        token: &str,
        original_dir: &Path,
    ) -> Result<()>;
    fn calculate_metadata(&self, dir: &Path) -> Result<(String, String)>;
}

pub struct RealCiProvider<'a> {
    shell: &'a dyn Shell,
    term_signal: Arc<AtomicBool>,
}

impl<'a> RealCiProvider<'a> {
    pub fn new(shell: &'a dyn Shell, term_signal: Arc<AtomicBool>) -> Self {
        Self { shell, term_signal }
    }
}

// Entry point
pub fn run(shell: &dyn Shell) -> Result<()> {
    let term_signal = Arc::new(AtomicBool::new(false));
    let r = term_signal.clone();
    ctrlc::set_handler(move || {
        r.store(true, Ordering::SeqCst);
    })
    .context("Error setting Ctrl-C handler")?;

    let provider = RealCiProvider::new(shell, term_signal);
    run_with_provider(&provider)
}

fn run_with_provider(provider: &impl CiProvider) -> Result<()> {
    // We rely on the provider's cancellation check instead of a local channel
    // because the provider might be blocking on user input.

    let token = provider.get_gitlab_token()?;
    let original_dir = provider.current_dir()?;
    let (host, project_path) = provider.get_project_info()?;
    let project_encoded = project_path.replace('/', "%2F");

    let is_clean = provider.is_git_clean(&original_dir)?;

    let (pipeline_id, web_url, pipeline_created_at, _workflow) = if is_clean {
        println!("Git checkout is clean. Checking for active pipeline on current HEAD...");
        let sha = provider.get_git_head(&original_dir)?;
        let branch = provider.get_git_branch(&original_dir)?;

        match provider.search_pipeline(
            &host,
            &project_encoded,
            &sha,
            Some(&branch),
            None,
            &token,
        )? {
            Some(pipeline) => {
                println!(
                    "  Found existing pipeline (ID: {}, Status: {})",
                    pipeline.id, pipeline.status
                );
                (pipeline.id, pipeline.web_url, pipeline.created_at, None)
            }
            None => {
                println!(
                    "  No existing pipeline found for {}. Triggering new pipeline on branch {}...",
                    sha, branch
                );

                let (target_branch, workflow) =
                    ensure_branch_pushed_with_provider(provider, &original_dir, &branch)?;

                let (id, url, created_at) =
                    provider.trigger_pipeline(&host, &project_encoded, &target_branch, &token)?;
                (id, url, created_at, workflow)
            }
        }
    } else {
        println!("Calculating project state metadata...");
        let (base_sha, diff_md5) = provider.calculate_metadata(&original_dir)?;
        println!("  Base hash: {}", base_sha);
        println!("  Diff MD5:  {}", diff_md5);

        println!("Checking for existing pipelines matching this state...");
        let existing = provider.search_pipeline(
            &host,
            &project_encoded,
            &base_sha,
            None,
            Some(&diff_md5),
            &token,
        )?;

        if let Some(pipeline) = existing {
            println!(
                "  Found existing pipeline (ID: {}, Status: {}, Branch: {})",
                pipeline.id, pipeline.status, pipeline.branch
            );
            (pipeline.id, pipeline.web_url, pipeline.created_at, None)
        } else {
            println!("  No matching pipeline found. Setting up temporary CI workflow...");
            let workflow = provider.setup_workflow(&original_dir, &base_sha, &diff_md5)?;
            let branch = workflow.branch.clone();
            let sha = workflow.sha.clone();

            println!("    Checking for pipeline on branch {}", branch);
            // Give GitLab a moment to start the pipeline
            sleep(Duration::from_secs(5));

            let (pipeline_id, web_url, created_at) = match provider.search_pipeline(
                &host,
                &project_encoded,
                &sha,
                Some(&branch),
                None,
                &token,
            )? {
                Some(pipeline) => (pipeline.id, pipeline.web_url, pipeline.created_at),
                None => {
                    println!("    No automatic pipeline found. Triggering new pipeline...");
                    provider.trigger_pipeline(&host, &project_encoded, &branch, &token)?
                }
            };
            (pipeline_id, web_url, created_at, Some(workflow))
        }
    };

    println!("  Pipeline URL: {}", web_url);
    println!("Waiting for pipeline to complete...");

    let mut job_cursors: HashMap<u64, usize> = HashMap::new();
    let start_time = Instant::now();
    let spinner_chars = ['|', '/', '-', '\\'];
    let mut spinner_idx = 0;

    let mut last_poll = Instant::now()
        .checked_sub(Duration::from_secs(10))
        .unwrap_or(start_time);
    let mut current_status = "initializing...".to_string();

    loop {
        if provider.is_canceled() {
            println!("\n  ! Received Ctrl-C, exiting (cleanup will run)...");
            break Err(anyhow::anyhow!("Interrupted by user"));
        }

        if last_poll.elapsed() >= Duration::from_secs(5) {
            if let Err(e) = provider.poll_logs(
                &host,
                &project_encoded,
                pipeline_id,
                &token,
                &mut job_cursors,
            ) {
                print!("\x1b[2K\r");
                println!("  ! Failed to poll logs: {}", e);
            }

            let status = match provider.check_pipeline_status(
                &host,
                &project_encoded,
                pipeline_id,
                &token,
            ) {
                Ok(s) => s,
                Err(_) => "unknown".to_string(),
            };

            current_status = status.clone();
            last_poll = Instant::now();

            match status.as_str() {
                "success" => {
                    println!("\n  ✓ Pipeline succeeded!");
                    break Ok(());
                }
                "failed" | "canceled" | "skipped" => {
                    println!("\n  x Pipeline ended with status: {}", status);
                    if status == "failed" {
                        provider.handle_failure(
                            &host,
                            &project_encoded,
                            pipeline_id,
                            &token,
                            &original_dir,
                        )?;
                    }
                    break Err(anyhow::anyhow!("Pipeline failed"));
                }
                "running"
                | "pending"
                | "created"
                | "waiting_for_resource"
                | "preparing"
                | "canceling" => {}
                _ => {
                    print!("\x1b[2K\r");
                    println!("  ? Unknown status: {}", status);
                }
            }
        }

        let elapsed: time::Duration = OffsetDateTime::now_utc() - pipeline_created_at;
        let timer = format!(
            "{:02}:{:02}",
            elapsed.whole_minutes(),
            elapsed.whole_seconds() % 60
        );
        let spinner = spinner_chars[spinner_idx % spinner_chars.len()];
        spinner_idx += 1;

        print!(
            "\r\x1b[2K{} Status: {} ({}) - {}",
            spinner, current_status, timer, web_url
        );
        std::io::stdout().flush()?;

        sleep(Duration::from_millis(500));
    }
}

pub struct Workflow {
    _temp_dir: Option<TempDir>,
    branch: String,
    sha: String,
}

#[derive(Debug, Clone)]
pub struct PipelineInfo {
    id: u64,
    web_url: String,
    status: String,
    branch: String,
    created_at: OffsetDateTime,
}

impl Workflow {
    fn setup(
        shell: &dyn Shell,
        original_dir: &Path,
        base_sha: &str,
        diff_md5: &str,
    ) -> Result<Self> {
        let temp_dir = tempfile::tempdir()?;
        let temp_path = temp_dir.path();

        // 1. Get original origin URL
        let repo_url = get_remote_url(shell, "origin")?;

        // 2. Copy project files (excluding target and .git)
        println!(
            "    Copying project to temp directory: {}",
            temp_path.display()
        );
        for entry in walkdir::WalkDir::new(original_dir)
            .into_iter()
            .filter_entry(|e| {
                let name = e.file_name().to_str().unwrap_or("");
                name != "target" && name != ".git"
            })
        {
            let entry = entry?;
            let rel_path = entry.path().strip_prefix(original_dir)?;
            let target_path = temp_path.join(rel_path);

            if entry.file_type().is_dir() {
                std::fs::create_dir_all(&target_path)?;
            } else {
                std::fs::copy(entry.path(), &target_path)?;
            }
        }

        // 3. Initialize git and commit
        let branch = format!("ci-test-{}", &Uuid::new_v4().to_string()[..8]);

        let git_temp = |args: &[&str]| -> Result<String> {
            let mut cmd = Command::new("git");
            cmd.args(args).current_dir(temp_path);
            let output = shell.output(&mut cmd)?;
            if !output.status.success() {
                bail!(
                    "Git command failed: git {:?} -> {}",
                    args,
                    String::from_utf8_lossy(&output.stderr)
                );
            }
            Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
        };

        git_temp(&["init"])?;
        git_temp(&["remote", "add", "origin", &repo_url])?;
        git_temp(&["checkout", "-b", &branch])?;
        git_temp(&["add", "."])?;
        git_temp(&["config", "user.email", "ci@temp.log"])?;
        git_temp(&["config", "user.name", "Temp CI Runner"])?;

        let commit_msg = format!(
            "Temp CI commit\n\nBase hash: {}\nDiff MD5: {}",
            base_sha, diff_md5
        );
        git_temp(&["commit", "-m", &commit_msg])?;

        let sha = git_temp(&["rev-parse", "HEAD"])?;

        println!("    Pushing branch {} to origin...", branch);
        git_temp(&["push", "origin", &branch])?;

        Ok(Self {
            _temp_dir: Some(temp_dir),
            branch,
            sha,
        })
    }
}

impl Drop for Workflow {
    fn drop(&mut self) {
        println!(
            "  Cleaning up temporary CI workflow (branch: {})...",
            self.branch
        );
        let _ = Command::new("git")
            .args(["push", "origin", "--delete", &self.branch])
            .status();
    }
}

fn search_pipeline_robust(
    shell: &dyn Shell,
    host: &str,
    project: &str,
    sha: &str,
    branch: Option<&str>,
    diff_md5: Option<&str>,
    token: &str,
) -> Result<Option<PipelineInfo>> {
    let target_sha = if let Some(dm) = diff_md5 {
        // Fetch recent commits across all branches and filter manually for metadata.
        let url = format!(
            "http://{}/api/v4/projects/{}/repository/commits?per_page=100&all=true",
            host, project
        );

        let output = curl_get(shell, &url, token)?;
        let commits: serde_json::Value =
            serde_json::from_str(&output).context("Failed to parse commits JSON")?;
        let commits_array = commits
            .as_array()
            .context("Commits response is not an array")?;

        let mut found_sha = None;
        for commit in commits_array {
            let message = commit["message"].as_str().unwrap_or("");
            let commit_sha = commit["id"].as_str().unwrap_or("");

            if message.contains(dm) && message.contains(sha) {
                found_sha = Some(commit_sha.to_string());
                break;
            }
        }
        found_sha
    } else {
        Some(sha.to_string())
    };

    if let Some(s) = target_sha {
        let pipe_url = format!(
            "http://{}/api/v4/projects/{}/pipelines?sha={}",
            host, project, s
        );

        let pipe_output = curl_get(shell, &pipe_url, token)?;
        let pipelines: serde_json::Value =
            serde_json::from_str(&pipe_output).context("Failed to parse pipelines JSON")?;

        return pick_pipeline(&pipelines, branch);
    }

    Ok(None)
}

fn pick_pipeline(
    pipelines: &serde_json::Value,
    branch: Option<&str>,
) -> Result<Option<PipelineInfo>> {
    let pipelines_array = pipelines
        .as_array()
        .context("Pipelines response is not an array")?;

    for pipeline in pipelines_array {
        let ref_name = pipeline["ref"].as_str().unwrap_or("unknown");

        // If branch is specified, we MUST match it to avoid race conditions
        // with other branches having the same SHA (e.g. merge commits or same base).
        if matches!(branch, Some(target_branch) if ref_name != target_branch) {
            continue;
        }

        let id = pipeline["id"].as_u64().context("Invalid pipeline ID")?;
        let web_url = pipeline["web_url"]
            .as_str()
            .unwrap_or("unknown")
            .to_string();
        let status = pipeline["status"].as_str().unwrap_or("unknown").to_string();
        let created_at_str = pipeline["created_at"]
            .as_str()
            .context("Missing created_at field")?;
        let created_at = OffsetDateTime::parse(created_at_str, &Rfc3339)
            .context("Failed to parse created_at timestamp")?;

        return Ok(Some(PipelineInfo {
            id,
            web_url,
            status,
            branch: ref_name.to_string(),
            created_at,
        }));
    }
    Ok(None)
}

fn trigger_new_pipeline(
    shell: &dyn Shell,
    host: &str,
    project_encoded: &str,
    branch: &str,
    token: &str,
) -> Result<(u64, String, OffsetDateTime)> {
    let trigger_url = format!(
        "http://{}/api/v4/projects/{}/pipeline?ref={}",
        host, project_encoded, branch
    );

    let output = curl_post(shell, &trigger_url, token)?;
    let initial_response: serde_json::Value =
        serde_json::from_str(&output).context("Failed to parse GitLab API response")?;

    let pipeline_id = initial_response["id"]
        .as_u64()
        .context("Failed to get pipeline ID")?;

    let web_url = initial_response["web_url"]
        .as_str()
        .unwrap_or("unknown URL")
        .to_string();

    let created_at_str = initial_response["created_at"]
        .as_str()
        .context("Missing created_at field in trigger response")?;
    let created_at = OffsetDateTime::parse(created_at_str, &Rfc3339)
        .context("Failed to parse created_at timestamp")?;

    println!("  Pipeline triggered! ID: {}", pipeline_id);
    Ok((pipeline_id, web_url, created_at))
}

fn poll_logs(
    shell: &dyn Shell,
    host: &str,
    project: &str,
    pipeline_id: u64,
    token: &str,
    cursors: &mut HashMap<u64, usize>,
) -> Result<()> {
    let jobs_url = format!(
        "http://{}/api/v4/projects/{}/pipelines/{}/jobs",
        host, project, pipeline_id
    );

    let jobs_json = curl_get(shell, &format!("{}?per_page=100", jobs_url), token)?;
    let jobs: serde_json::Value =
        serde_json::from_str(&jobs_json).context("Failed to parse jobs JSON")?;
    let jobs_array = jobs.as_array().context("Jobs response is not an array")?;

    let mut relevant_jobs: Vec<&serde_json::Value> = jobs_array
        .iter()
        .filter(|job| {
            let status = job["status"].as_str().unwrap_or("unknown");
            status == "running" || status == "failed" || status == "success"
        })
        .collect();

    // Sort by started_at, falling back to created_at
    relevant_jobs.sort_by(|a, b| {
        let a_time = a["started_at"]
            .as_str()
            .or_else(|| a["created_at"].as_str())
            .unwrap_or("");
        let b_time = b["started_at"]
            .as_str()
            .or_else(|| b["created_at"].as_str())
            .unwrap_or("");
        a_time.cmp(b_time)
    });

    for job in relevant_jobs {
        let job_id = job["id"].as_u64().context("Invalid job ID")?;
        let job_name = job["name"].as_str().unwrap_or("unknown");

        let trace_url = format!(
            "http://{}/api/v4/projects/{}/jobs/{}/trace",
            host, project, job_id
        );

        // Ignore errors here to avoid interrupting the main loop for one job's failure
        if let Ok(trace) = curl_get(shell, &trace_url, token) {
            let lines: Vec<&str> = trace.lines().collect();
            let cursor = cursors.entry(job_id).or_insert(0);

            if lines.len() > *cursor {
                // clear current line (status)
                print!("\x1b[2K\r");
                for line in &lines[*cursor..] {
                    println!("[{}] {}", job_name, line);
                }
                *cursor = lines.len();
            }
        }
    }
    Ok(())
}

fn curl_download(
    shell: &dyn Shell,
    url: &str,
    token: &str,
    output_path: &std::path::Path,
) -> Result<()> {
    let mut cmd = Command::new("curl");
    cmd.arg("-s")
        .arg("--header")
        .arg(format!("PRIVATE-TOKEN: {}", token))
        .arg("-o")
        .arg(output_path)
        .arg(url);
    let output = shell.output(&mut cmd).context("Failed to execute curl")?;

    if !output.status.success() {
        bail!("curl download failed");
    }

    Ok(())
}

fn download_artifacts(
    shell: &dyn Shell,
    host: &str,
    project: &str,
    pipeline_id: u64,
    token: &str,
    original_dir: &Path,
) -> Result<()> {
    println!("\n  ? Checking for artifacts from failed jobs...");

    let jobs_url = format!(
        "http://{}/api/v4/projects/{}/pipelines/{}/jobs",
        host, project, pipeline_id
    );

    let jobs_json = curl_get(shell, &format!("{}?per_page=100", jobs_url), token)?;
    let jobs: serde_json::Value =
        serde_json::from_str(&jobs_json).context("Failed to parse jobs JSON")?;
    let jobs_array = jobs.as_array().context("Jobs response is not an array")?;

    let mut artifact_jobs = Vec::new();

    for job in jobs_array {
        let status = job["status"].as_str().unwrap_or("unknown");
        if status == "failed" {
            let job_id = job["id"].as_u64().context("Invalid job ID")?;
            let job_name = job["name"].as_str().unwrap_or("unknown");

            // Check if job has artifacts
            if let Some(_artifacts) = job["artifacts"].as_array().filter(|a| !a.is_empty()) {
                artifact_jobs.push((job_id, job_name.to_string()));
            }
        }
    }

    if artifact_jobs.is_empty() {
        println!("  No artifacts found in failed jobs.");
        return Ok(());
    }

    println!(
        "  Found artifacts in {} failed job(s):",
        artifact_jobs.len()
    );
    for (_, name) in &artifact_jobs {
        println!("    - {}", name);
    }

    print!("  Do you want to download and apply golden updates? [y/N] ");
    std::io::stdout().flush()?;

    let mut input = String::new();
    std::io::stdin().read_line(&mut input)?;
    let ans = input.trim().to_lowercase();

    if ans != "y" && ans != "yes" {
        return Ok(());
    }

    for (job_id, job_name) in artifact_jobs {
        println!("  Downloading artifacts for {}...", job_name);
        let artifacts_url = format!(
            "http://{}/api/v4/projects/{}/jobs/{}/artifacts",
            host, project, job_id
        );

        let temp_dir = std::env::temp_dir();
        let zip_path = temp_dir.join(format!("artifacts_{}.zip", job_id));

        if let Err(e) = curl_download(shell, &artifacts_url, token, &zip_path) {
            println!("  x Failed to download artifacts: {}", e);
            continue;
        }

        println!("  Extracting golden updates...");
        let file = std::fs::File::open(&zip_path)?;
        let mut archive = zip::ZipArchive::new(file)?;

        let target_dir = original_dir.join("crates/test-e2e/goldens");
        let mut count = 0;

        for i in 0..archive.len() {
            let mut file = archive.by_index(i)?;
            let name = file.name().to_string();

            if name.starts_with("target/golden_updates/")
                && name.ends_with(".png")
                && std::path::Path::new(&name).file_name().is_some()
            {
                let file_name = std::path::Path::new(&name).file_name().unwrap();
                let dest_path = target_dir.join(file_name);
                let mut dest_file = std::fs::File::create(&dest_path)?;
                std::io::copy(&mut file, &mut dest_file)?;
                println!("    Updated: {}", file_name.to_string_lossy());
                count += 1;
            }
        }

        println!("  ✓ Applied {} golden update(s) from {}", count, job_name);

        // Cleanup
        std::fs::remove_file(zip_path).unwrap_or(());
    }

    println!("  ✨ Golden images updated. Please review changes with `git status`.");

    Ok(())
}
fn get_gitlab_project_info(shell: &dyn Shell) -> Result<(String, String)> {
    let remotes = ["gitlab", "origin"];
    for remote in remotes {
        if let Some(url) = get_remote_url(shell, remote)
            .ok()
            .filter(|u| u.contains("gitlab.lan"))
        {
            let host = "gitlab.lan";
            let path_part = if url.starts_with("http") {
                url.split("gitlab.lan/").nth(1).unwrap_or("")
            } else if url.contains("gitlab.lan:") {
                url.split("gitlab.lan:").nth(1).unwrap_or("")
            } else {
                ""
            };
            let path = path_part.trim_end_matches(".git");
            if !path.is_empty() {
                return Ok((host.to_string(), path.to_string()));
            }
        }
    }
    bail!("Could not find GitLab remote")
}

fn get_remote_url(shell: &dyn Shell, remote: &str) -> Result<String> {
    let mut cmd = Command::new("git");
    cmd.args(["remote", "get-url", remote]);
    let output = shell.output(&mut cmd)?;
    if output.status.success() {
        Ok(String::from_utf8(output.stdout)?.trim().to_string())
    } else {
        bail!("Remote not found")
    }
}

fn curl_post(shell: &dyn Shell, url: &str, token: &str) -> Result<String> {
    let mut cmd = Command::new("curl");
    cmd.arg("-s")
        .arg("--header")
        .arg(format!("PRIVATE-TOKEN: {}", token))
        .arg("-X")
        .arg("POST")
        .arg(url);
    let output = shell.output(&mut cmd)?;
    if !output.status.success() {
        bail!("curl post failed");
    }
    Ok(String::from_utf8(output.stdout)?)
}

fn curl_get(shell: &dyn Shell, url: &str, token: &str) -> Result<String> {
    let mut cmd = Command::new("curl");
    cmd.arg("-s")
        .arg("--header")
        .arg(format!("PRIVATE-TOKEN: {}", token))
        .arg(url);
    let output = shell.output(&mut cmd)?;
    if !output.status.success() {
        bail!("curl get failed");
    }
    Ok(String::from_utf8(output.stdout)?)
}

fn print_failed_jobs(
    shell: &dyn Shell,
    host: &str,
    project: &str,
    pipeline_id: u64,
    token: &str,
) -> Result<()> {
    println!("\n  ! Fetching failure logs...");
    let jobs_url = format!(
        "http://{}/api/v4/projects/{}/pipelines/{}/jobs",
        host, project, pipeline_id
    );
    let jobs_json = curl_get(shell, &format!("{}?per_page=100", jobs_url), token)?;
    let jobs: serde_json::Value = serde_json::from_str(&jobs_json)?;
    let jobs_array = jobs.as_array().context("Jobs response is not an array")?;

    for job in jobs_array {
        let status = job["status"].as_str().unwrap_or("unknown");
        if status == "failed" || status == "canceled" {
            let job_id = job["id"].as_u64().unwrap();
            let job_name = job["name"].as_str().unwrap_or("unknown");
            println!(
                "\n  --- Job: {} (ID: {}, Status: {}) ---",
                job_name, job_id, status
            );
            let trace_url = format!(
                "http://{}/api/v4/projects/{}/jobs/{}/trace",
                host, project, job_id
            );
            if let Ok(trace) = curl_get(shell, &trace_url, token) {
                let lines: Vec<&str> = trace.lines().collect();
                let start = lines.len().saturating_sub(2000);
                for line in &lines[start..] {
                    println!("  {}", line);
                }
            }
        }
    }
    Ok(())
}

fn is_git_clean(shell: &dyn Shell, dir: &Path) -> Result<bool> {
    let mut cmd = Command::new("git");
    cmd.args(["status", "--porcelain"]).current_dir(dir);
    let output = shell.output(&mut cmd)?;
    Ok(output.stdout.is_empty())
}

fn get_git_head(shell: &dyn Shell, dir: &Path) -> Result<String> {
    let mut cmd = Command::new("git");
    cmd.args(["rev-parse", "HEAD"]).current_dir(dir);
    let output = shell.output(&mut cmd)?;
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

fn get_git_branch(shell: &dyn Shell, dir: &Path) -> Result<String> {
    let mut cmd = Command::new("git");
    cmd.args(["rev-parse", "--abbrev-ref", "HEAD"])
        .current_dir(dir);
    let output = shell.output(&mut cmd)?;
    Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
}

fn ensure_branch_pushed_with_provider(
    provider: &impl CiProvider,
    dir: &Path,
    branch: &str,
) -> Result<(String, Option<Workflow>)> {
    let local_sha = provider.get_git_head(dir)?;
    let remote_sha = provider.check_remote_branch_sha(dir, branch)?;

    if local_sha == remote_sha {
        return Ok((branch.to_string(), None));
    }

    println!(
        "  ! Local branch '{}' is not pushed to origin or is outdated.",
        branch
    );
    if remote_sha.is_empty() {
        println!("    (Remote branch does not exist)");
    } else {
        let local_short = if local_sha.len() >= 8 {
            &local_sha[..8]
        } else {
            &local_sha
        };
        let remote_short = if remote_sha.len() >= 8 {
            &remote_sha[..8]
        } else {
            &remote_sha
        };
        println!("    (Local: {}, Remote: {})", local_short, remote_short);
    }

    println!("  How would you like to proceed?");
    let options = vec![
        "Push to a temporary branch (default)".to_string(),
        format!("Push to '{}'", branch),
        "Do nothing (pipeline may fail or run on old commit)".to_string(),
    ];
    let choice_idx = provider.prompt_user("Enter choice", &options, 0)?;

    match choice_idx {
        1 => {
            println!("  Pushing to '{}'...", branch);
            provider.git_push(dir, branch)?;
            Ok((branch.to_string(), None))
        }
        2 => {
            println!("  Continuing without push...");
            Ok((branch.to_string(), None))
        }
        _ => {
            // Default to 0
            let temp_branch = format!("ci-temp-{}-{}", branch, &Uuid::new_v4().to_string()[..8]);
            println!("  Pushing to temporary branch '{}'...", temp_branch);
            provider.git_push_temp(dir, branch, &temp_branch)?;

            let workflow = Workflow {
                _temp_dir: None,
                branch: temp_branch.clone(),
                sha: local_sha,
            };

            Ok((temp_branch, Some(workflow)))
        }
    }
}

// Implement RealCiProvider

impl<'a> CiProvider for RealCiProvider<'a> {
    fn current_dir(&self) -> Result<std::path::PathBuf> {
        self.shell.current_dir()
    }

    fn get_gitlab_token(&self) -> Result<String> {
        std::env::var("GITLAB_TOKEN")
            .or_else(|_| {
                self.shell
                    .read(Path::new(".token"))
                    .map(|s| s.trim().to_string())
            })
            .context("GITLAB_TOKEN environment variable is not set and .token file not found")
    }

    fn get_project_info(&self) -> Result<(String, String)> {
        get_gitlab_project_info(self.shell)
    }

    fn is_git_clean(&self, dir: &Path) -> Result<bool> {
        is_git_clean(self.shell, dir)
    }

    fn get_git_head(&self, dir: &Path) -> Result<String> {
        get_git_head(self.shell, dir)
    }

    fn get_git_branch(&self, dir: &Path) -> Result<String> {
        get_git_branch(self.shell, dir)
    }

    fn check_remote_branch_sha(&self, dir: &Path, branch: &str) -> Result<String> {
        let mut cmd = Command::new("git");
        cmd.args(["rev-parse", &format!("origin/{}", branch)])
            .current_dir(dir);
        let output = self.shell.output(&mut cmd)?;

        if output.status.success() {
            Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
        } else {
            Ok(String::new())
        }
    }

    fn is_canceled(&self) -> bool {
        self.term_signal.load(Ordering::SeqCst)
    }

    fn prompt_user(&self, _message: &str, options: &[String], default_idx: usize) -> Result<usize> {
        for (i, opt) in options.iter().enumerate() {
            println!("    {}) {}", i, opt);
        }
        print!("  Enter choice [{}]: ", default_idx);
        std::io::stdout().flush()?;

        let (tx, rx) = std::sync::mpsc::channel();
        std::thread::spawn(move || {
            let mut input = String::new();
            if std::io::stdin().read_line(&mut input).is_ok() {
                let _ = tx.send(input);
            }
        });

        loop {
            if self.is_canceled() {
                println!(); // New line
                return Err(anyhow::anyhow!("Interrupted by user"));
            }

            if let Ok(input) = rx.try_recv() {
                let choice = input.trim();
                if choice.is_empty() {
                    return Ok(default_idx);
                }
                if let Some(idx) = choice.parse::<usize>().ok().filter(|&i| i < options.len()) {
                    return Ok(idx);
                }
                return Ok(default_idx); // fallback
            }

            sleep(Duration::from_millis(100));
        }
    }

    fn git_push(&self, dir: &Path, branch: &str) -> Result<()> {
        let mut cmd = Command::new("git");
        cmd.args(["push", "origin", branch]).current_dir(dir);
        let status = self.shell.run(&mut cmd)?;
        if !status.success() {
            bail!("Failed to push branch");
        }
        Ok(())
    }

    fn git_push_temp(&self, dir: &Path, _local_branch: &str, temp_branch_name: &str) -> Result<()> {
        let mut cmd = Command::new("git");
        cmd.args([
            "push",
            "origin",
            format!("HEAD:{}", temp_branch_name).as_str(),
        ])
        .current_dir(dir);
        let status = self.shell.run(&mut cmd)?;
        if !status.success() {
            bail!("Failed to push temporary branch");
        }
        Ok(())
    }

    fn trigger_pipeline(
        &self,
        host: &str,
        project: &str,
        branch: &str,
        token: &str,
    ) -> Result<(u64, String, OffsetDateTime)> {
        trigger_new_pipeline(self.shell, host, project, branch, token)
    }

    fn search_pipeline(
        &self,
        host: &str,
        project_encoded: &str,
        sha: &str,
        branch: Option<&str>,
        diff_md5: Option<&str>,
        token: &str,
    ) -> Result<Option<PipelineInfo>> {
        search_pipeline_robust(
            self.shell,
            host,
            project_encoded,
            sha,
            branch,
            diff_md5,
            token,
        )
    }

    fn setup_workflow(
        &self,
        original_dir: &Path,
        base_sha: &str,
        diff_md5: &str,
    ) -> Result<Workflow> {
        Workflow::setup(self.shell, original_dir, base_sha, diff_md5)
    }

    fn poll_logs(
        &self,
        host: &str,
        project_encoded: &str,
        pipeline_id: u64,
        token: &str,
        cursors: &mut HashMap<u64, usize>,
    ) -> Result<()> {
        poll_logs(
            self.shell,
            host,
            project_encoded,
            pipeline_id,
            token,
            cursors,
        )
    }

    fn check_pipeline_status(
        &self,
        host: &str,
        project_encoded: &str,
        pipeline_id: u64,
        token: &str,
    ) -> Result<String> {
        let poll_url = format!(
            "http://{}/api/v4/projects/{}/pipelines/{}",
            host, project_encoded, pipeline_id
        );
        let output = curl_get(self.shell, &poll_url, token)?;
        let response: serde_json::Value =
            serde_json::from_str(&output).context("Failed to parse pipeline status")?;

        let status = response["status"]
            .as_str()
            .context("Failed to get pipeline status")?;
        Ok(status.to_string())
    }

    fn handle_failure(
        &self,
        host: &str,
        project_encoded: &str,
        pipeline_id: u64,
        token: &str,
        original_dir: &Path,
    ) -> Result<()> {
        print_failed_jobs(self.shell, host, project_encoded, pipeline_id, token)?;
        if let Err(e) = download_artifacts(
            self.shell,
            host,
            project_encoded,
            pipeline_id,
            token,
            original_dir,
        ) {
            println!("  ! Failed to download artifacts: {}", e);
        }
        Ok(())
    }

    fn calculate_metadata(&self, dir: &Path) -> Result<(String, String)> {
        // We reuse the logic, but adapted to use self.shell
        let git_orig = |args: &[&str]| -> Result<String> {
            let mut cmd = Command::new("git");
            cmd.args(args).current_dir(dir);
            let output = self.shell.output(&mut cmd)?;
            if !output.status.success() {
                bail!("Git command failed on original dir");
            }
            Ok(String::from_utf8_lossy(&output.stdout).trim().to_string())
        };

        let base_sha = git_orig(&["rev-parse", "HEAD"])?;
        let diff = git_orig(&["diff"])?;

        use md5::{Digest, Md5};
        let mut hasher = Md5::new();
        hasher.update(diff.as_bytes());
        let diff_md5 = hex::encode(hasher.finalize());

        Ok((base_sha, diff_md5))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::shell::MockShell;
    use std::cell::RefCell;

    struct MockCiProvider {
        is_clean: bool,
        local_head: String,
        remote_branch_sha: String,
        git_branch: String,
        prompt_responses: RefCell<Vec<usize>>,
        push_log: RefCell<Vec<String>>,
        existing_pipeline: RefCell<Option<PipelineInfo>>,
        pipeline_status: RefCell<String>,
    }

    impl MockCiProvider {
        fn new() -> Self {
            Self {
                is_clean: true,
                local_head: "sha_local".to_string(),
                remote_branch_sha: "sha_remote".to_string(), // Mismatch by default
                git_branch: "feature/foo".to_string(),
                prompt_responses: RefCell::new(vec![]),
                push_log: RefCell::new(vec![]),
                existing_pipeline: RefCell::new(None),
                pipeline_status: RefCell::new("success".to_string()),
            }
        }
    }

    impl CiProvider for MockCiProvider {
        fn current_dir(&self) -> Result<std::path::PathBuf> {
            Ok(std::path::PathBuf::from("/tmp/mock"))
        }

        fn get_gitlab_token(&self) -> Result<String> {
            Ok("mock_token".to_string())
        }

        fn get_project_info(&self) -> Result<(String, String)> {
            Ok(("gitlab.lan".to_string(), "group/project".to_string()))
        }

        fn is_git_clean(&self, _dir: &Path) -> Result<bool> {
            Ok(self.is_clean)
        }

        fn get_git_head(&self, _dir: &Path) -> Result<String> {
            Ok(self.local_head.clone())
        }

        fn get_git_branch(&self, _dir: &Path) -> Result<String> {
            Ok(self.git_branch.clone())
        }

        fn check_remote_branch_sha(&self, _dir: &Path, _branch: &str) -> Result<String> {
            Ok(self.remote_branch_sha.clone())
        }

        fn prompt_user(
            &self,
            _message: &str,
            _options: &[String],
            default_idx: usize,
        ) -> Result<usize> {
            let mut responses = self.prompt_responses.borrow_mut();
            if responses.is_empty() {
                Ok(default_idx)
            } else {
                Ok(responses.remove(0))
            }
        }

        fn git_push(&self, _dir: &Path, branch: &str) -> Result<()> {
            self.push_log
                .borrow_mut()
                .push(format!("push origin {}", branch));
            Ok(())
        }

        fn git_push_temp(
            &self,
            _dir: &Path,
            _local_branch: &str,
            temp_branch_name: &str,
        ) -> Result<()> {
            self.push_log
                .borrow_mut()
                .push(format!("push origin HEAD:{}", temp_branch_name));
            Ok(())
        }

        fn trigger_pipeline(
            &self,
            _host: &str,
            _project: &str,
            _branch: &str,
            _token: &str,
        ) -> Result<(u64, String, OffsetDateTime)> {
            Ok((123, "http://url".to_string(), OffsetDateTime::now_utc()))
        }

        fn search_pipeline(
            &self,
            _host: &str,
            _project: &str,
            _sha: &str,
            _branch: Option<&str>,
            _diff_md5: Option<&str>,
            _token: &str,
        ) -> Result<Option<PipelineInfo>> {
            Ok(self.existing_pipeline.borrow().clone())
        }

        fn setup_workflow(&self, _dir: &Path, _base: &str, _diff: &str) -> Result<Workflow> {
            Ok(Workflow {
                _temp_dir: None,
                branch: "temp-workflow".to_string(),
                sha: "mock_sha".to_string(),
            })
        }

        fn poll_logs(
            &self,
            _host: &str,
            _project: &str,
            _id: u64,
            _token: &str,
            _cursors: &mut HashMap<u64, usize>,
        ) -> Result<()> {
            Ok(())
        }

        fn check_pipeline_status(
            &self,
            _host: &str,
            _project: &str,
            _id: u64,
            _token: &str,
        ) -> Result<String> {
            Ok(self.pipeline_status.borrow().clone())
        }

        fn handle_failure(
            &self,
            _host: &str,
            _project: &str,
            _id: u64,
            _token: &str,
            _dir: &Path,
        ) -> Result<()> {
            Ok(())
        }

        fn calculate_metadata(&self, _dir: &Path) -> Result<(String, String)> {
            Ok(("mock_base_sha".to_string(), "mock_diff_md5".to_string()))
        }

        fn is_canceled(&self) -> bool {
            false
        }
    }

    #[test]
    fn test_ensure_branch_pushed_clean_synced() {
        let mut provider = MockCiProvider::new();
        provider.local_head = "sha1".to_string();
        provider.remote_branch_sha = "sha1".to_string();

        let (branch, workflow) =
            ensure_branch_pushed_with_provider(&provider, Path::new("."), "feature/foo").unwrap();

        assert_eq!(branch, "feature/foo");
        assert!(workflow.is_none());
        assert!(provider.push_log.borrow().is_empty());
    }

    #[test]
    fn test_ensure_branch_pushed_mismatch_push_temp() {
        let mut provider = MockCiProvider::new();
        provider.local_head = "sha_local".to_string();
        provider.remote_branch_sha = "sha_remote".to_string();
        provider.prompt_responses.borrow_mut().push(0); // Option 0: Push to temp

        let (branch, workflow) =
            ensure_branch_pushed_with_provider(&provider, Path::new("."), "feature/foo").unwrap();

        assert!(branch.starts_with("ci-temp-feature/foo-"));
        assert!(workflow.is_some());
        assert_eq!(workflow.unwrap().branch, branch);

        // Check push log
        let logs = provider.push_log.borrow();
        assert_eq!(logs.len(), 1);
        assert!(logs[0].contains("push origin HEAD:ci-temp-feature/foo-"));
    }

    #[test]
    fn test_ensure_branch_pushed_mismatch_push_current() {
        let mut provider = MockCiProvider::new();
        provider.local_head = "sha_local".to_string();
        provider.remote_branch_sha = "sha_remote".to_string();
        provider.prompt_responses.borrow_mut().push(1); // Option 1: Push to current

        let (branch, workflow) =
            ensure_branch_pushed_with_provider(&provider, Path::new("."), "feature/foo").unwrap();

        assert_eq!(branch, "feature/foo");
        assert!(workflow.is_none());

        let logs = provider.push_log.borrow();
        assert_eq!(logs.len(), 1);
        assert_eq!(logs[0], "push origin feature/foo");
    }

    #[test]
    fn test_ensure_branch_pushed_mismatch_do_nothing() {
        let mut provider = MockCiProvider::new();
        provider.local_head = "sha_local".to_string();
        provider.remote_branch_sha = "sha_remote".to_string();
        provider.prompt_responses.borrow_mut().push(2); // Option 2: Do nothing

        let (branch, workflow) =
            ensure_branch_pushed_with_provider(&provider, Path::new("."), "feature/foo").unwrap();

        assert_eq!(branch, "feature/foo");
        assert!(workflow.is_none());
        assert!(provider.push_log.borrow().is_empty());
    }

    #[test]
    fn test_pick_pipeline_filtering() {
        let json = serde_json::json!([
            {
                "id": 1,
                "ref": "other-branch",
                "web_url": "http://url/1",
                "status": "success",
                "created_at": "2024-01-01T00:00:00Z"
            },
            {
                "id": 2,
                "ref": "main",
                "web_url": "http://url/2",
                "status": "success",
                "created_at": "2024-01-01T00:00:01Z"
            }
        ]);

        // Should skip first one and find second one when filtered by 'main'
        let res = pick_pipeline(&json, Some("main")).unwrap().unwrap();
        assert_eq!(res.id, 2);

        // Should pick first one when not filtered
        let res = pick_pipeline(&json, None).unwrap().unwrap();
        assert_eq!(res.id, 1);

        // Should find nothing for non-existent branch
        let res = pick_pipeline(&json, Some("missing")).unwrap();
        assert!(res.is_none());
    }
    #[test]
    fn test_run_with_provider_full_flow() {
        // Test the main run loop with a clean git state and mismatching remote (triggering new pipeline)
        let provider = MockCiProvider::new();
        // Prompt will default to 0 (Push Temp)
        // We mocked search_pipeline to return None, so it will trigger a new pipeline.
        // We mocked trigger_pipeline to return success.
        // We mocked check_pipeline_status to return success.

        let result = run_with_provider(&provider);
        assert!(result.is_ok());

        let logs = provider.push_log.borrow();
        // Should have pushed to temp branch
        assert!(logs.iter().any(|l| l.contains("push origin HEAD:ci-temp-")));
    }

    #[test]
    fn test_run_with_provider_dirty_checkout() {
        let mut provider = MockCiProvider::new();
        provider.is_clean = false; // Dirty
        *provider.pipeline_status.borrow_mut() = "success".to_string();

        let result = run_with_provider(&provider);
        assert!(result.is_ok());

        // Should have set up workflow (mocked search returns None)
        let logs = provider.push_log.borrow();
        assert!(logs.is_empty()); // MockCiProvider::setup_workflow doesn't log pushes
    }

    #[test]
    fn test_run_with_provider_existing_pipeline() {
        let provider = MockCiProvider::new();
        let existing = PipelineInfo {
            id: 999,
            web_url: "http://existing".to_string(),
            status: "running".to_string(),
            branch: "feature/foo".to_string(),
            created_at: OffsetDateTime::now_utc(),
        };
        *provider.existing_pipeline.borrow_mut() = Some(existing);

        let result = run_with_provider(&provider);
        assert!(result.is_ok());

        // Should NOT have pushed since it attached to existing
        assert!(provider.push_log.borrow().is_empty());
    }

    #[test]
    fn test_run_with_provider_pipeline_failure() {
        let provider = MockCiProvider::new();
        *provider.pipeline_status.borrow_mut() = "failed".to_string();

        let result = run_with_provider(&provider);
        assert!(result.is_err());
        assert_eq!(result.unwrap_err().to_string(), "Pipeline failed");
    }

    #[test]
    fn test_run_with_provider_pipeline_canceled() {
        let provider = MockCiProvider::new();
        *provider.pipeline_status.borrow_mut() = "canceled".to_string();

        let result = run_with_provider(&provider);
        assert!(result.is_err());
        assert_eq!(result.unwrap_err().to_string(), "Pipeline failed");
    }

    #[test]
    fn test_calculate_metadata_with_mock_shell() {
        let shell = MockShell::new();
        // 1. git rev-parse HEAD
        shell.push_output(b"mock_sha\n", b"", true);
        // 2. git diff
        shell.push_output(b"some diff content", b"", true);

        let term_signal = Arc::new(AtomicBool::new(false));
        let provider = RealCiProvider::new(&shell, term_signal);
        let dir = Path::new("/mock/dir");
        let (sha, diff_md5) = provider.calculate_metadata(dir).unwrap();

        assert_eq!(sha, "mock_sha");

        use md5::{Digest, Md5};
        let mut hasher = Md5::new();
        hasher.update(b"some diff content");
        let expected_md5 = hex::encode(hasher.finalize());
        assert_eq!(diff_md5, expected_md5);

        // Verify commands
        let commands = shell.recorded_commands.lock().unwrap();
        assert_eq!(commands.len(), 2);
        assert!(commands[0].contains("rev-parse"));
        assert!(commands[1].contains("diff"));
    }

    #[test]
    fn test_search_pipeline_robust_found() {
        let shell = MockShell::new();
        // 1. Commit search response (empty array mostly, but let's say we find one)
        // search_pipeline_robust calls commits?per_page=100...
        let commits_json = serde_json::json!([
            {
                "id": "commit_sha",
                "message": "diff_md5:mock_diff\nbase_sha:mock_base"
            }
        ]);
        shell.push_output(commits_json.to_string().as_bytes(), b"", true);

        // 2. Pipeline search response
        let pipelines_json = serde_json::json!([
            {
                "id": 100,
                "ref": "feature/branch",
                "web_url": "http://url",
                "status": "pending",
                "created_at": "2024-01-01T00:00:00Z"
            }
        ]);
        shell.push_output(pipelines_json.to_string().as_bytes(), b"", true);

        let res = search_pipeline_robust(
            &shell,
            "host",
            "project",
            "mock_base",
            Some("feature/branch"),
            Some("mock_diff"),
            "token",
        )
        .unwrap();

        assert!(res.is_some());
        let pipeline = res.unwrap();
        assert_eq!(pipeline.id, 100);
        assert_eq!(pipeline.branch, "feature/branch");
    }

    #[test]
    fn test_trigger_new_pipeline_success() {
        let shell = MockShell::new();
        // Response for POST trigger
        let trigger_json = serde_json::json!({
            "id": 200,
            "web_url": "http://url/200",
            "status": "created",
            "created_at": "2024-01-02T00:00:00Z"
        });
        shell.push_output(trigger_json.to_string().as_bytes(), b"", true);

        let (id, url, _) =
            trigger_new_pipeline(&shell, "host", "project", "feature/new", "token").unwrap();

        assert_eq!(id, 200);
        assert_eq!(url, "http://url/200");
    }

    #[test]
    fn test_check_pipeline_status_success() {
        let shell = MockShell::new();
        let status_json = serde_json::json!({
            "id": 300,
            "status": "success",
            "web_url": "http://url/300"
        });
        shell.push_output(status_json.to_string().as_bytes(), b"", true);

        let term_signal = Arc::new(AtomicBool::new(false));
        let provider = RealCiProvider::new(&shell, term_signal);
        let status = provider
            .check_pipeline_status("host", "project", 300, "token")
            .unwrap();

        assert_eq!(status, "success");
    }

    #[test]
    fn test_poll_logs_logic() {
        let shell = MockShell::new();
        let mut cursors = HashMap::new();

        // 1. Jobs list response
        let jobs_json = serde_json::json!([
            {
                "id": 400,
                "name": "test-job",
                "status": "running",
                "started_at": "2024-01-01T00:00:00Z"
            }
        ]);
        shell.push_output(jobs_json.to_string().as_bytes(), b"", true);

        // 2. Trace response
        shell.push_output(b"line 1\nline 2\n", b"", true);

        poll_logs(&shell, "host", "project", 123, "token", &mut cursors).unwrap();

        assert_eq!(cursors.get(&400), Some(&2));

        // Next poll with more data
        shell.push_output(jobs_json.to_string().as_bytes(), b"", true);
        shell.push_output(b"line 1\nline 2\nline 3\n", b"", true);

        poll_logs(&shell, "host", "project", 123, "token", &mut cursors).unwrap();
        assert_eq!(cursors.get(&400), Some(&3));
    }

    #[test]
    fn test_print_failed_jobs_logic() {
        let shell = MockShell::new();
        // 1. Jobs response with one failed job
        let jobs_json = serde_json::json!([
            {
                "id": 500,
                "name": "failed-job",
                "status": "failed"
            },
            {
                "id": 501,
                "name": "success-job",
                "status": "success"
            }
        ]);
        shell.push_output(jobs_json.to_string().as_bytes(), b"", true);

        // 2. Trace response for failed job
        shell.push_output(b"error log line 1\nerror log line 2\n", b"", true);

        let result = print_failed_jobs(&shell, "host", "project", 123, "token");
        assert!(result.is_ok());

        // Verify commands: 1 for jobs, 1 for trace
        let commands = shell.recorded_commands.lock().unwrap();
        assert_eq!(commands.len(), 2);
        assert!(commands[0].contains("pipelines/123/jobs"));
        assert!(commands[1].contains("jobs/500/trace"));
    }

    #[test]
    fn test_real_ci_provider_get_token_from_file() {
        let shell = MockShell::new();
        shell.push_read_result(Ok("   secret_token \n".to_string()));

        // Ensure env var is NOT set for this test
        unsafe { std::env::remove_var("GITLAB_TOKEN") };

        let term_signal = Arc::new(AtomicBool::new(false));
        let provider = RealCiProvider::new(&shell, term_signal);
        let token = provider.get_gitlab_token().unwrap();
        assert_eq!(token, "secret_token");
    }

    #[test]
    fn test_real_ci_provider_get_token_fail() {
        let shell = MockShell::new();
        // mock read to fail
        shell.push_read_result(Err("File not found".to_string()));
        unsafe { std::env::remove_var("GITLAB_TOKEN") };

        let term_signal = Arc::new(AtomicBool::new(false));
        let provider = RealCiProvider::new(&shell, term_signal);
        let result = provider.get_gitlab_token();
        assert!(result.is_err());
    }

    #[test]
    fn test_get_remote_url_success() {
        let shell = MockShell::new();
        shell.push_output(b"https://gitlab.lan/foo/bar.git\n", b"", true);

        let url = get_remote_url(&shell, "origin").unwrap();
        assert_eq!(url, "https://gitlab.lan/foo/bar.git");
    }

    #[test]
    fn test_get_gitlab_project_info_success() {
        let shell = MockShell::new();
        // first origin fails, then gitlab succeeds
        shell.push_output(b"", b"error", false); // origin
        shell.push_output(b"git@gitlab.lan:group/proj.git\n", b"", true); // gitlab

        let (host, path) = get_gitlab_project_info(&shell).unwrap();
        assert_eq!(host, "gitlab.lan");
        assert_eq!(path, "group/proj");
    }

    #[test]
    fn test_calculate_metadata_git_fail() {
        let shell = MockShell::new();
        shell.push_output(b"", b"git error", false); // rev-parse fails

        let term_signal = Arc::new(AtomicBool::new(false));
        let provider = RealCiProvider::new(&shell, term_signal);
        let result = provider.calculate_metadata(Path::new("."));
        assert!(result.is_err());
        assert!(
            result
                .unwrap_err()
                .to_string()
                .contains("Git command failed")
        );
    }

    #[test]
    fn test_workflow_setup_logic() {
        let shell = MockShell::new();
        let original_dir = tempfile::tempdir().unwrap();
        let root = original_dir.path();

        // TODO Let's add expects for these, rather than just pushing outputs.

        // Setup workflow logic calls:
        // 1. get_remote_url(shell, "origin") (in Workflow::setup)
        shell.push_output(b"https://gitlab.lan/repo.git\n", b"", true);
        // 2. git init
        shell.push_output(b"", b"", true);
        // 3. git remote add origin
        shell.push_output(b"", b"", true);
        // 4. git checkout -b
        shell.push_output(b"", b"", true);
        // 5. git add .
        shell.push_output(b"", b"", true);
        // 6. git config user.email
        shell.push_output(b"", b"", true);
        // 7. git config user.name
        shell.push_output(b"", b"", true);
        // 8. git commit
        shell.push_output(b"", b"", true);
        // 9. git rev-parse HEAD
        shell.push_output(b"new_sha\n", b"", true);
        // 10. git push origin
        shell.push_output(b"", b"", true);

        // We explicitly don't want the drop cleanup to run during the test if possible,
        // or we just let it fail silently (it uses Command::new which is real).
        // Since we are in a mock environment, it will likely fail or do nothing.

        let workflow = Workflow::setup(&shell, root, "base_sha", "diff_md5").unwrap();

        assert_eq!(workflow.sha, "new_sha");
        assert!(workflow.branch.starts_with("ci-test-"));

        let commands = shell.recorded_commands.lock().unwrap();
        assert!(
            commands
                .iter()
                .any(|c| c.contains("push") && c.contains("origin"))
        );
    }
}
