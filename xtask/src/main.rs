//! Build automation for splug audio plugin
//!
//! Implements cargo-xtask pattern for polyglot build process:
//! - Rust compilation
//! - Shader compilation (GLSL → SPIR-V)
//! - Platform-specific bundle creation
//! - Codesigning (macOS)

use anyhow::{Context as _, Result, bail};
use clap::{Args, Parser, Subcommand};

use std::process::Command;
mod ci;
mod environment;
mod shell;

#[cfg(test)]
mod main_tests;

use shell::{RealShell, Shell};
use std::sync::Mutex;
use std::sync::OnceLock;
use uuid::Uuid;

#[derive(Parser, Debug)]
#[command(name = "xtask")]
#[command(about = "Build automation for splug", long_about = None)]
pub struct Cli {
    #[command(subcommand)]
    pub command: Commands,
}

#[derive(Subcommand, Debug)]
pub enum Commands {
    /// Run clippy and fail on warnings
    Lint,
    /// Run coverage analysis
    Coverage(CoverageArgs),
    /// Build the project
    Build(BuildArgs),
    /// Run tests
    Test(TestArgs),
    /// Trigger GitLab CI pipeline and wait for results
    Ci,
    /// Build/Serve the Intro Book
    Book {
        /// Install mdbook if missing
        #[arg(long)]
        install: bool,
        /// Serve the book locally instead of just building
        #[arg(long)]
        serve: bool,
    },
    /// Run mutation tests using cargo-mutants
    Mutants(MutantsArgs),
}

#[derive(Args, Debug, Clone)]
pub struct BuildArgs {
    /// Build in release mode
    #[arg(long)]
    pub release: bool,
    /// Build Docker image for E2E tests
    #[arg(long)]
    pub docker: bool,
}

#[derive(Args, Debug, Clone)]
pub struct TestArgs {
    /// Run tests and coverage (default), or only tests with --quick
    #[arg(long)]
    pub quick: bool,
    /// Run all tests
    #[arg(long)]
    pub all: bool,
    /// Run tests inside Docker container (Default on Linux)
    #[arg(long)]
    pub docker: bool,
    /// Run tests inside Tart VM (Default on macOS)
    #[arg(long)]
    pub vm: bool,
    /// Name of the Tart VM to use
    #[arg(long, default_value = "vst-test-vm")]
    pub vm_name: String,
    /// Disable backtrace for failed tests
    #[arg(long)]
    pub no_backtrace: bool,
    /// Enable full backtrace (RUST_BACKTRACE=full)
    #[arg(long)]
    pub full_backtrace: bool,
    /// Arguments to pass through to the inner command
    #[arg(last = true)]
    pub passthrough: Vec<String>,
}

#[derive(Args, Debug, Clone)]
pub struct CoverageArgs {
    /// Skip coverage verification (90%)
    #[arg(long)]
    pub no_verify: bool,
    /// Run all modes
    #[arg(long)]
    pub all: bool,
    /// Run coverage inside Docker container
    #[arg(long)]
    pub docker: bool,
    /// Run coverage inside Tart VM
    #[arg(long)]
    pub vm: bool,
    /// Name of the Tart VM to use
    #[arg(long, default_value = "vst-test-vm")]
    pub vm_name: String,
    /// Disable backtrace for failed tests
    #[arg(long)]
    pub no_backtrace: bool,
    /// Enable full backtrace (RUST_BACKTRACE=full)
    #[arg(long)]
    pub full_backtrace: bool,
    /// Output a text coverage report to `target/coverage-{uuid}.txt`
    #[arg(long)]
    pub text_output: bool,
    /// Open the coverage report in the browser (generates HTML)
    #[arg(long)]
    pub open: bool,
    /// Arguments to pass through to the inner command
    #[arg(last = true)]
    pub passthrough: Vec<String>,
}

#[derive(Args, Debug, Clone)]
pub struct MutantsArgs {
    /// Run mutants natively on host
    #[arg(long)]
    pub native: bool,
    /// Run mutants inside Docker container
    #[arg(long)]
    pub docker: bool,
    /// Run mutants inside Tart VM
    #[arg(long)]
    pub vm: bool,
    /// Run in all available environments
    #[arg(long)]
    pub all: bool,
    /// Name of the Tart VM to use
    #[arg(long, default_value = "vst-test-vm")]
    pub vm_name: String,
    /// Disable backtrace for failed tests
    #[arg(long)]
    pub no_backtrace: bool,
    /// Enable full backtrace (RUST_BACKTRACE=full)
    #[arg(long)]
    pub full_backtrace: bool,
    /// Arguments to pass through to the inner command
    #[arg(last = true)]
    pub passthrough: Vec<String>,
}

pub struct XtaskContext<'a> {
    pub shell: &'a dyn Shell,
}

impl<'a> XtaskContext<'a> {
    pub fn new(shell: &'a dyn Shell) -> Self {
        Self { shell }
    }
}

/// Main entry point for xtask.
fn main() -> Result<()> {
    let cli = Cli::parse();
    let shell = RealShell;
    let ctx = XtaskContext::new(&shell);
    run_cli(cli, &ctx)
}

/// Execute the requested CLI command.
pub fn run_cli(cli: Cli, ctx: &XtaskContext) -> Result<()> {
    match cli.command {
        Commands::Lint => lint(ctx),
        Commands::Coverage(args) => coverage(ctx, args),
        Commands::Build(args) => build(ctx, args),
        Commands::Test(args) => test(ctx, args),
        Commands::Mutants(args) => mutants(ctx, args),
        Commands::Ci => {
            use crate::ci::run;
            run(ctx.shell)
        }
        Commands::Book { install, serve } => book(ctx, install, serve),
    }
}

/// Build/Serve the mdBook documentation.
fn book(ctx: &XtaskContext, install: bool, serve: bool) -> Result<()> {
    if install {
        println!("Installing mdbook...");
        let mut cmd = Command::new("cargo");
        cmd.args(["install", "mdbook"]);
        let status = ctx
            .shell
            .run(&mut cmd)
            .context("Failed to install mdbook")?;
        if !status.success() {
            bail!("Failed to install mdbook");
        }
    }

    // Check if mdbook is installed
    let mut check_cmd = Command::new("mdbook");
    check_cmd.arg("--version");

    if !ctx.shell.run(&mut check_cmd).unwrap().success() {
        println!("mdbook not found. Attempting to install...");
        // Recursive call with install=true
        return book(ctx, true, serve);
    }

    println!("Building Intro Book...");
    let mut cmd = Command::new("mdbook");
    if serve {
        cmd.args(["serve", "docs/intro-book", "--open"]);
    } else {
        cmd.args(["build", "docs/intro-book"]);
    }
    cmd.args(["--dest-dir", "target/intro-book"]);

    let status = ctx.shell.run(&mut cmd).context("Failed to run mdbook")?;

    if !status.success() {
        bail!("mdbook build failed");
    }

    if !serve {
        let path = std::path::Path::new("docs/intro-book/book/index.html");
        if path.exists() {
            println!("  ✓ Book built at: {}", path.display());
            println!("  To view as single page, open the built book and use the Print icon.");
        }
    }

    Ok(())
}

static CLEANUP_GUARDS: OnceLock<Mutex<Vec<CleanupTask>>> = OnceLock::new();

#[derive(Clone)]
pub(crate) struct VmProjectGuard {
    pub ip: String,
    pub remote_path: String,
}

impl Drop for VmProjectGuard {
    fn drop(&mut self) {
        println!("Cleaning up remote directory: {}", self.remote_path);
        let _ = Command::new("sshpass")
            .args([
                "-p",
                "admin",
                "ssh",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                &format!("admin@{}", self.ip),
                &format!("rm -rf {}", self.remote_path),
            ])
            .output();
    }
}

pub(crate) struct DockerGuard {
    pub container_name: String,
}

impl Drop for DockerGuard {
    fn drop(&mut self) {
        println!("Killing Docker container: {}", self.container_name);
        let _ = Command::new("docker")
            .args(["kill", &self.container_name])
            .output();
    }
}

pub(crate) enum CleanupTask {
    Vm(VmProjectGuard),
    Docker(DockerGuard),
}

pub(crate) fn register_guard(task: CleanupTask) {
    let mut guards = CLEANUP_GUARDS
        .get_or_init(|| Mutex::new(Vec::new()))
        .lock()
        .unwrap();
    guards.push(task);
}

fn register_ctrlc_handler() {
    let _ = ctrlc::set_handler(move || {
        println!("\nReceived Ctrl-C, cleaning up...");
        if let Some(mutex) = CLEANUP_GUARDS.get()
            && let Ok(mut guards) = mutex.lock()
        {
            for guard in guards.drain(..) {
                match guard {
                    CleanupTask::Vm(v) => drop(v),
                    CleanupTask::Docker(d) => drop(d),
                }
            }
        }
        std::process::exit(130);
    });
}

/// Build the project.
fn build(ctx: &XtaskContext, args: BuildArgs) -> Result<()> {
    // TODO: Build the docker image as part of the test command
    if args.docker {
        println!("Building Docker image...");
        let dockerfile = if cfg!(target_arch = "aarch64") {
            "docker-build/aarch64/Dockerfile"
        } else {
            "docker-build/x64/Dockerfile"
        };

        let mut cmd = Command::new("docker");
        cmd.args([
            "build",
            "-f",
            dockerfile,
            "-t",
            "simcity-ci",
            "docker-build/",
        ]);
        let status = ctx
            .shell
            .run(&mut cmd)
            .context("Failed to run docker build")?;

        if !status.success() {
            bail!("Docker build failed");
        }
        println!("  ✓ Docker image built: simcity-ci ({})", dockerfile);
    } else {
        println!("Building workspace...");
        let mut cmd = Command::new("cargo");
        cmd.arg("build").arg("--workspace");
        if args.release {
            cmd.arg("--release");
        }
        let status = ctx
            .shell
            .run(&mut cmd)
            .context("Failed to run cargo build")?;
        if !status.success() {
            bail!("Build failed");
        }
        println!("  ✓ Build complete");
    }
    Ok(())
}

/// Run doctests.
fn doctests(ctx: &XtaskContext) -> Result<()> {
    println!("Running doctests...");
    let mut cmd = Command::new("cargo");
    cmd.args(["test", "--doc", "--workspace"]);
    let status = ctx.shell.run(&mut cmd).context("Failed to run cargo doc")?;
    if !status.success() {
        println!();
        bail!("  ⚠ Docs build warning (workspace may have limited library targets)");
    }
    println!("  ✓ Docs build complete");
    Ok(())
}

/// Build documentation.
fn docs(ctx: &XtaskContext) -> Result<()> {
    println!("Building docs...");
    let mut cmd = Command::new("cargo");
    cmd.args(["doc", "--no-deps", "--lib", "--workspace"]);
    let status = ctx.shell.run(&mut cmd).context("Failed to run cargo doc")?;
    if !status.success() {
        println!();
        bail!("  ⚠ Docs build failed");
    }
    println!("  ✓ Docs build complete");
    Ok(())
}

/// Run tests.
///
/// Runs `cargo fix`, `cargo clippy`, `cargo doc`, and then executes tests
/// in the specified environment (Host, Docker, or Tart).
#[allow(clippy::too_many_arguments)]
fn test(ctx: &XtaskContext, args: TestArgs) -> Result<()> {
    fix(ctx)?;
    lint(ctx)?;
    docs(ctx)?;
    doctests(ctx)?;
    println!("Running tests...");

    let modes = get_modes(/*native=*/ false, args.docker, args.vm, args.all);

    for mode in modes {
        run_command(
            ctx,
            mode,
            &args.vm_name,
            XtaskCommand::Test {
                no_backtrace: args.no_backtrace,
                full_backtrace: args.full_backtrace,
                no_default_features: args.quick,
                passthrough: args.passthrough.clone(),
            },
        )?;
    }

    if !args.quick {
        println!("\nTests passed, now running coverage...");
        // Construct CoverageArgs for default coverage run
        let cov_args = CoverageArgs {
            no_verify: false,
            all: args.all,
            docker: args.docker,
            vm: args.vm,
            vm_name: args.vm_name, // reusing same VM name
            no_backtrace: args.no_backtrace,
            full_backtrace: args.full_backtrace,
            text_output: false,
            open: false,
            passthrough: args.passthrough,
        };
        coverage(ctx, cov_args)?;
    }

    Ok(())
}

/// Run `cargo fix`.
fn fix(ctx: &XtaskContext) -> Result<()> {
    println!("Running cargo fix...");
    let mut cmd = Command::new("cargo");
    cmd.args(["fix", "--all-targets", "--workspace", "--allow-dirty"]);
    let status = ctx.shell.run(&mut cmd).context("Failed to run cargo fix")?;
    if !status.success() {
        bail!("Cargo fix failed");
    }
    println!("  ✓ Cargo fix passed");
    Ok(())
}

/// Run `cargo clippy`.
fn lint(ctx: &XtaskContext) -> Result<()> {
    println!("Running clippy...");
    let mut cmd = Command::new("cargo");
    cmd.args([
        "clippy",
        "--all-targets",
        "--workspace",
        "--",
        "-D",
        "warnings",
    ]);
    let status = ctx.shell.run(&mut cmd).context("Failed to run clippy")?;
    if !status.success() {
        bail!("Clippy failed");
    }
    println!("  ✓ Clippy passed");
    Ok(())
}

/// Run coverage analysis.
#[allow(clippy::too_many_arguments)]
fn coverage(ctx: &XtaskContext, args: CoverageArgs) -> Result<()> {
    println!("Running coverage analysis...");

    let text_output_path = if args.text_output {
        Some(format!("coverage-{}.txt", Uuid::new_v4()))
    } else {
        None
    };

    let lcov_output_path = if args.open {
        Some(format!("coverage-{}.lcov", Uuid::new_v4()))
    } else {
        None
    };

    let html_output_dir = if args.open {
        Some(format!("coverage-html-{}", Uuid::new_v4()))
    } else {
        None
    };

    let modes = get_modes(/*native=*/ false, args.docker, args.vm, args.all);

    for mode in modes {
        run_command(
            ctx,
            mode,
            &args.vm_name,
            XtaskCommand::Coverage {
                no_verify: args.no_verify,
                no_backtrace: args.no_backtrace,
                full_backtrace: args.full_backtrace,
                text_output_path: text_output_path.clone(),
                lcov_output_path: lcov_output_path.clone(),
                html_output_dir: html_output_dir.clone(),
                host_root: None,
                open: args.open,
                passthrough: args.passthrough.clone(),
            },
        )?;
    }

    if let Some(path) = text_output_path {
        println!("  Text report generated at: target/{}", path);
    }

    Ok(())
}

/// Run mutation tests.
fn mutants(ctx: &XtaskContext, args: MutantsArgs) -> Result<()> {
    println!("Running mutation tests...");

    let mut modes = get_modes(args.native, args.docker, args.vm, args.all);
    // Default for mutants is VM + Docker if nothing specified
    if !args.docker && !args.vm && !args.native && !args.all {
        modes = vec![TestMode::Tart, TestMode::Docker];
    }

    for mode in modes {
        run_command(
            ctx,
            mode,
            &args.vm_name,
            XtaskCommand::Mutants {
                no_backtrace: args.no_backtrace,
                full_backtrace: args.full_backtrace,
                passthrough: args.passthrough.clone(),
            },
        )?;
    }

    Ok(())
}

fn get_modes(native: bool, docker: bool, vm: bool, all: bool) -> Vec<TestMode> {
    let mut modes: Vec<TestMode> = vec![];
    if (native | all) && std::env::var("CI").is_ok() {
        modes.push(TestMode::Native);
    }
    if docker | all {
        modes.push(TestMode::Docker);
    }
    if vm | all {
        modes.push(TestMode::Tart);
    }
    if modes.is_empty() {
        // Defaults: Tart on macOS, Docker on Linux
        #[cfg(target_os = "macos")]
        modes.push(TestMode::Tart);
        modes.push(TestMode::Docker);
    }
    modes
}

#[derive(Clone)]
pub(crate) enum XtaskCommand {
    Test {
        no_backtrace: bool,
        full_backtrace: bool,
        no_default_features: bool,
        passthrough: Vec<String>,
    },
    Coverage {
        no_verify: bool,
        no_backtrace: bool,
        full_backtrace: bool,
        text_output_path: Option<String>,
        lcov_output_path: Option<String>,
        html_output_dir: Option<String>,
        #[allow(dead_code)]
        host_root: Option<String>,
        open: bool,
        passthrough: Vec<String>,
    },
    Mutants {
        no_backtrace: bool,
        full_backtrace: bool,
        passthrough: Vec<String>,
    },
}

impl XtaskCommand {
    pub(crate) fn description(&self) -> &str {
        match self {
            XtaskCommand::Test { .. } => "tests",
            XtaskCommand::Coverage { .. } => "coverage analysis",
            XtaskCommand::Mutants { .. } => "mutation tests",
        }
    }

    pub(crate) fn to_remote_command(&self, mode: TestMode) -> String {
        let mut envs = String::from("CARGO_XTASK_TEST_REQUIRED=true");
        if let TestMode::Tart = mode {
            // Add /usr/local/lib for Vulkan/MoltenVK on macOS VM
            envs.push_str(" DYLD_LIBRARY_PATH=/usr/local/lib");
            envs.push_str(" VK_ICD_FILENAMES=/usr/local/share/vulkan/icd.d/MoltenVK_icd.json");
        }
        if let Ok(log) = std::env::var("RUST_LOG") {
            envs.push_str(&format!(" RUST_LOG={}", log));
        }
        if let Ok(ci) = std::env::var("CI") {
            envs.push_str(&format!(" CI={}", ci));
        }
        match self {
            XtaskCommand::Test {
                no_backtrace,
                full_backtrace,
                no_default_features,
                passthrough,
            } => {
                if *full_backtrace {
                    envs.push_str(" RUST_BACKTRACE=full");
                } else if !*no_backtrace {
                    envs.push_str(" RUST_BACKTRACE=1");
                }
                let mut cmd = format!("{} cargo nextest run", envs);
                if *no_default_features {
                    cmd.push_str(" --no-default-features");
                }
                if passthrough.is_empty() {
                    cmd.push_str(" --workspace");
                    cmd.push_str(" --no-fail-fast");
                }
                for arg in passthrough {
                    cmd.push_str(&format!(" {}", arg));
                }
                cmd
            }
            XtaskCommand::Coverage {
                no_verify,
                no_backtrace,
                full_backtrace,
                text_output_path,
                lcov_output_path,
                html_output_dir,
                host_root,
                open: _,
                passthrough,
            } => {
                if *full_backtrace {
                    envs.push_str(" RUST_BACKTRACE=full");
                } else if !*no_backtrace {
                    envs.push_str(" RUST_BACKTRACE=1");
                }
                if let Some(host) = host_root {
                    envs.push_str(&format!(" CARGO_XTASK_HOST_ROOT={}", host));
                }
                let mut cmd = format!("{} cargo llvm-cov nextest --all-targets", envs);
                if !*no_verify {
                    cmd.push_str(" --fail-under-lines 85");
                }
                if passthrough.is_empty() {
                    cmd.push_str(" --workspace");
                    cmd.push_str(" --no-fail-fast");
                }
                for arg in passthrough {
                    cmd.push_str(&format!(" {}", arg));
                }

                let mut report_cmds = String::new();
                if let Some(path) = text_output_path {
                    report_cmds.push_str(&format!(
                        "; cargo llvm-cov report --text --output-path target/{}",
                        path
                    ));
                }
                if let Some(path) = lcov_output_path {
                    report_cmds.push_str(&format!(
                        "; cargo llvm-cov report --lcov --output-path target/{}",
                        path
                    ));
                }
                if let Some(dir) = html_output_dir {
                    report_cmds.push_str(&format!(
                        "; cargo llvm-cov report --html --output-dir target/{}",
                        dir
                    ));
                }

                if !report_cmds.is_empty() {
                    // Wrap in braces to decouple from previous && chains in the caller,
                    // and use ; to ensure report runs even if tests fail.
                    // Capture exit code, generate reports, then exit with original code.
                    cmd = format!("{{ {}; XT_EXIT=$?{}; exit $XT_EXIT; }}", cmd, report_cmds);
                }
                cmd
            }
            XtaskCommand::Mutants {
                no_backtrace,
                full_backtrace,
                passthrough,
            } => {
                let mut envs = String::from("CARGO_XTASK_TEST_REQUIRED=true");
                if let Ok(log) = std::env::var("RUST_LOG") {
                    envs.push_str(&format!(" RUST_LOG={}", log));
                }
                envs.push_str(" DYLD_LIBRARY_PATH=/usr/local/lib");
                envs.push_str(" VK_ICD_FILENAMES=/usr/local/share/vulkan/icd.d/MoltenVK_icd.json");

                if *full_backtrace {
                    envs.push_str(" RUST_BACKTRACE=full");
                } else if !*no_backtrace {
                    envs.push_str(" RUST_BACKTRACE=1");
                }

                let mut cmd = format!("{} cargo mutants --workspace", envs);
                if passthrough.is_empty() {
                    cmd.push_str(" --no-times");
                }
                for arg in passthrough {
                    cmd.push_str(&format!(" {}", arg));
                }
                cmd
            }
        }
    }

    fn requirements_bash(&self) -> String {
        match self {
            XtaskCommand::Test { .. } => String::from(
                "if ! command -v cargo-nextest &> /dev/null; then \
                    echo 'Installing cargo-nextest...'; \
                    curl -LsSf https://get.nexte.st/latest/linux | tar zxf - -C /usr/local/bin || cargo install cargo-nextest --locked; \
                fi",
            ),
            XtaskCommand::Coverage { .. } => String::from(
                "if ! command -v cargo-llvm-cov &> /dev/null; then \
                    echo 'Installing cargo-llvm-cov...'; \
                    cargo install cargo-llvm-cov --locked; \
                fi && \
                rustup component add llvm-tools-preview",
            ),
            XtaskCommand::Mutants { .. } => String::from(
                "if ! command -v cargo-mutants &> /dev/null; then \
                    echo 'Installing cargo-mutants...'; \
                    cargo install cargo-mutants --locked; \
                fi",
            ),
        }
    }
}
fn run_command(ctx: &XtaskContext, mode: TestMode, vm_name: &str, cmd: XtaskCommand) -> Result<()> {
    match mode {
        TestMode::Docker => {
            use crate::environment::{DockerEnvironment, ExecutionEnvironment};

            let env = DockerEnvironment::new();
            env.build(ctx)?;
            let success = env.execute(ctx, &cmd)?;

            if !success {
                bail!("{} failed in Docker", cmd.description());
            }
        }
        TestMode::Tart => {
            use crate::environment::{ExecutionEnvironment, TartVMEnvironment};

            let env = TartVMEnvironment::new(vm_name.to_string());
            env.build(ctx)?;
            let success = env.execute(ctx, &cmd)?;

            if !success {
                bail!("{} failed in VM", cmd.description());
            }
        }
        TestMode::Native => {
            use std::process::Command;

            println!("Running {} natively...", cmd.description());
            println!("{}", cmd.to_remote_command(mode));

            // On host, we just execute bash -c with the command string
            let mut host_cmd = Command::new("bash");
            host_cmd.arg("-c").arg(cmd.to_remote_command(mode));

            let status = ctx
                .shell
                .run(&mut host_cmd)
                .context("Failed to run native command")?;

            if !status.success() {
                bail!("{} failed natively", cmd.description());
            }
        }
    }
    Ok(())
}

/// Post-process coverage reports (LCOV, HTML) to fix paths when running remotely.
fn process_coverage_reports(
    ctx: &XtaskContext,
    lcov_path: &Option<String>,
    html_dir: &Option<String>,
    remote_root: &str,
    open: bool,
) -> Result<()> {
    if !open {
        return Ok(());
    }

    let pwd = ctx.shell.current_dir()?;
    let local_root = pwd.to_string_lossy().to_string();

    if let Some(path) = lcov_path {
        let host_path = pwd.join("target").join(path);
        if ctx.shell.exists(&host_path) {
            println!("  Fixing paths in LCOV report...");
            let content = ctx.shell.read(&host_path)?;
            let fixed_content = content
                .replace(remote_root, &local_root)
                .replace(&format!("/private{}", remote_root), &local_root);
            ctx.shell.write(&host_path, &fixed_content)?;
        }
    }

    if let Some(dir_name) = html_dir {
        let host_dir = pwd.join("target").join(dir_name);
        if ctx.shell.exists(&host_dir) {
            println!("  Fixing paths in HTML coverage report...");

            let html_content_dir = if ctx.shell.exists(&host_dir.join("html")) {
                host_dir.join("html")
            } else {
                host_dir.clone()
            };

            // Relocate directories first
            let coverage_dir = html_content_dir.join("coverage");
            if ctx.shell.exists(&coverage_dir) {
                let source_path = remote_root.strip_prefix('/').unwrap_or(remote_root);
                let source_dir = coverage_dir.join(source_path);
                let private_source_dir = coverage_dir.join("private").join(source_path);

                let actual_source = if ctx.shell.exists(&source_dir) {
                    Some(source_dir)
                } else if ctx.shell.exists(&private_source_dir) {
                    Some(private_source_dir)
                } else {
                    None
                };

                if let Some(src) = actual_source {
                    let target_path = local_root.strip_prefix('/').unwrap_or(&local_root);
                    let target_dir = coverage_dir.join(target_path);

                    if src != target_dir {
                        println!("  Relocating HTML source files to match host root...");
                        let _ = ctx.shell.create_dir_all(target_dir.parent().unwrap());
                        let _ = ctx.shell.rename(&src, &target_dir);

                        // Clean up empty directories
                        let _ = ctx.shell.remove_dir_all(&coverage_dir.join("private"));
                        let _ = ctx.shell.remove_dir_all(&coverage_dir.join("tmp"));
                    }
                }
            }

            // Now fix paths in all HTML files
            fix_html_paths(
                ctx.shell,
                &html_content_dir,
                &html_content_dir,
                remote_root,
                &local_root,
            )?;

            let index_html = if ctx.shell.exists(&html_content_dir.join("index.html")) {
                html_content_dir.join("index.html")
            } else {
                host_dir.join("index.html")
            };

            println!("  ✓ HTML report ready: {}", index_html.display());
            println!("  Opening browser...");
            let mut open_cmd = Command::new("open");
            open_cmd.arg(index_html);
            let _ = ctx.shell.run(&mut open_cmd);
        }
    }

    Ok(())
}

fn fix_html_paths(
    shell: &dyn Shell,
    path: &std::path::Path,
    html_root: &std::path::Path,
    remote_root: &str,
    local_root: &str,
) -> Result<()> {
    if path.is_dir() {
        for entry in std::fs::read_dir(path)? {
            fix_html_paths(shell, &entry?.path(), html_root, remote_root, local_root)?;
        }
    } else if path.extension().and_then(|s| s.to_str()) == Some("html") {
        let content = shell.read(path)?;
        let mut fixed = content.replace(remote_root, local_root);
        if remote_root.starts_with('/') {
            fixed = fixed.replace(&format!("/private{}", remote_root), local_root);
        }
        // Also handle the case where the LCOV already had the host path but maybe with /private
        if local_root.starts_with("/Users") {
            let private_local = format!("/private{}", local_root);
            fixed = fixed.replace(&private_local, local_root);
        }

        // Fix relative asset links (style.css, control.js)
        if let Ok(rel_path) = path.parent().unwrap().strip_prefix(html_root) {
            let depth = rel_path.components().count();
            let mut prefix = String::new();
            for _ in 0..depth {
                prefix.push_str("../");
            }

            fixed = fix_asset_links(&fixed, "style.css", &prefix);
            fixed = fix_asset_links(&fixed, "control.js", &prefix);
        }

        if fixed != content {
            shell.write(path, &fixed)?;
        }
    }
    Ok(())
}

fn fix_asset_links(content: &str, filename: &str, new_prefix: &str) -> String {
    let mut output = String::with_capacity(content.len());
    let mut last_end = 0;

    let mut i = 0;
    while let Some(pos) = content[i..].find(filename) {
        let actual_pos = i + pos;
        // Look back for a quote
        let search_limit = actual_pos.saturating_sub(50);
        if let Some(quote_pos) = content[search_limit..actual_pos].rfind(['\'', '"']) {
            let actual_quote_pos = search_limit + quote_pos;
            let bracket_content = &content[actual_quote_pos + 1..actual_pos];

            // Is it a relative path (empty or only ../)?
            let is_relative = bracket_content.is_empty()
                || (bracket_content.starts_with("../")
                    && bracket_content.chars().all(|c| c == '.' || c == '/'));

            if is_relative {
                let context = &content[search_limit..actual_quote_pos];
                if context.ends_with("href=") || context.ends_with("src=") {
                    output.push_str(&content[last_end..actual_quote_pos + 1]);
                    output.push_str(new_prefix);
                    output.push_str(filename);
                    last_end = actual_pos + filename.len();
                }
            }
        }
        i = actual_pos + filename.len();
        if i >= content.len() {
            break;
        }
    }
    output.push_str(&content[last_end..]);
    output
}

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub(crate) enum TestMode {
    Docker,
    Tart,
    Native,
}

fn get_vm_ip(shell: &dyn Shell, name: &str) -> Result<String> {
    for _ in 0..30 {
        let mut cmd = Command::new("tart");
        cmd.args(["ip", name]);
        let output = shell.output(&mut cmd)?;

        if output.status.success() {
            let ip = String::from_utf8(output.stdout)?.trim().to_string();
            if !ip.is_empty() {
                return Ok(ip);
            }
        }
        std::thread::sleep(std::time::Duration::from_secs(1));
    }
    bail!("Timed out waiting for VM IP")
}

fn wait_for_ssh(shell: &dyn Shell, ip: &str) -> Result<()> {
    println!("  Waiting for SSH availability...");
    let start = std::time::Instant::now();
    while start.elapsed().as_secs() < 15 {
        let mut cmd = Command::new("sshpass");
        cmd.args([
            "-p",
            "admin",
            "ssh",
            "-o",
            "ConnectTimeout=1",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            &format!("admin@{}", ip),
            "exit 0",
        ])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null());

        let status = shell.run(&mut cmd);

        if status.is_ok_and(|s| s.success()) {
            println!("  ✓ SSH operational");
            return Ok(());
        }
        std::thread::sleep(std::time::Duration::from_secs(1));
    }
    bail!("VM unresponsive to SSH (timed out after 15s)")
}
