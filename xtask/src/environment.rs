use crate::shell::Shell;
use crate::{XtaskCommand, XtaskContext};
use anyhow::Result;

/// Trait for abstracting different execution environments (Docker, TartVM, etc.)
#[allow(dead_code)]
pub trait ExecutionEnvironment: Send + Sync {
    /// Build/prepare the execution environment (e.g., docker build, VM start)
    fn build(&self, ctx: &XtaskContext) -> Result<()>;

    /// Execute a command in the environment.
    ///
    /// Returns `Ok(true)` if the command succeeded, `Ok(false)` if it failed,
    /// and `Err` if the execution start failed.
    fn execute(&self, ctx: &XtaskContext, cmd: &XtaskCommand) -> Result<bool>;

    /// Check if this environment is available on the system
    fn is_available(&self, shell: &dyn Shell) -> bool;

    /// Get environment name for logging
    fn name(&self) -> &str;

    /// Get a description of what the environment is doing
    fn description(&self) -> String {
        format!("Running in {}", self.name())
    }
}

/// Docker execution environment.
///
/// Runs commands inside a Docker container, mapping source code and artifact
/// directories.
pub struct DockerEnvironment {
    image_name: String,
}

impl DockerEnvironment {
    pub fn new() -> Self {
        Self {
            image_name: "simcity-ci".to_string(),
        }
    }

    #[allow(dead_code)]
    pub fn with_image_name(mut self, name: String) -> Self {
        self.image_name = name;
        self
    }
}

impl Default for DockerEnvironment {
    fn default() -> Self {
        Self::new()
    }
}

impl ExecutionEnvironment for DockerEnvironment {
    fn build(&self, ctx: &XtaskContext) -> Result<()> {
        use anyhow::{Context as _, bail};
        use std::process::Command;

        println!("Building Docker image...");
        let dockerfile = if cfg!(target_arch = "aarch64") {
            "docker-build/aarch64/Dockerfile"
        } else {
            "docker-build/x64/Dockerfile"
        };

        let mut build_cmd = Command::new("docker");
        build_cmd.args([
            "build",
            "-f",
            dockerfile,
            "-t",
            &self.image_name,
            "docker-build/",
        ]);

        let status = ctx
            .shell
            .run(&mut build_cmd)
            .context("Failed to run docker build")?;

        if !status.success() {
            bail!("Docker build failed");
        }

        println!(
            "  ✓ Docker image built: {} ({})",
            self.image_name, dockerfile
        );
        Ok(())
    }

    fn execute(&self, ctx: &XtaskContext, cmd: &XtaskCommand) -> Result<bool> {
        use anyhow::{Context as _, bail};
        use std::process::Command;

        println!("Running {} in Docker container...", cmd.description());
        println!("{}", cmd.to_remote_command(crate::TestMode::Docker));

        let pwd = ctx.shell.current_dir()?;
        let pwd_str = pwd.to_str().context("Invalid path")?;

        // Store sccache in host target/sccache
        let sccache_dir = pwd.join("target").join("sccache");
        ctx.shell
            .create_dir_all(&sccache_dir)
            .context("Failed to create sccache dir")?;
        let sccache_dir_str = sccache_dir.to_str().context("Invalid sccache path")?;

        // Prepare Cargo cache directories on host
        let cargo_registry_index = pwd
            .join("target")
            .join("cargo")
            .join("registry")
            .join("index");
        let cargo_registry_cache = pwd
            .join("target")
            .join("cargo")
            .join("registry")
            .join("cache");
        let cargo_git_db = pwd.join("target").join("cargo").join("git").join("db");

        ctx.shell
            .create_dir_all(&cargo_registry_index)
            .context("Failed to create cargo registry index")?;
        ctx.shell
            .create_dir_all(&cargo_registry_cache)
            .context("Failed to create cargo registry cache")?;
        ctx.shell
            .create_dir_all(&cargo_git_db)
            .context("Failed to create cargo git db")?;

        let cargo_registry_index_str = cargo_registry_index
            .to_str()
            .context("Invalid cargo registry index path")?;
        let cargo_registry_cache_str = cargo_registry_cache
            .to_str()
            .context("Invalid cargo registry cache path")?;
        let cargo_git_db_str = cargo_git_db.to_str().context("Invalid cargo git db path")?;

        let inner_cmd = cmd.to_remote_command(crate::TestMode::Docker);
        let requirements = cmd.requirements_bash();

        let bash_cmd = format!(
            "{} && \\
             Xvfb :99 -screen 0 1024x768x24 & \\
             export DISPLAY=:99 && \\
             for i in $(seq 1 60); do \\
                 if xdpyinfo >/dev/null 2>&1; then \\
                     break; \\
                 fi; \\
                 sleep 0.5; \\
             done && \\
             {}",
            requirements, inner_cmd
        );

        // Create directory on host for golden updates
        let temp_dir = pwd.join("target").join("golden_updates");
        ctx.shell
            .create_dir_all(&temp_dir)
            .context("Failed to create golden update dir")?;
        let temp_dir_str = temp_dir.to_str().context("Invalid temp path")?;

        let icd_path = if cfg!(target_arch = "aarch64") {
            "/usr/share/vulkan/icd.d/lvp_icd.aarch64.json"
        } else {
            "/usr/share/vulkan/icd.d/lvp_icd.x86_64.json"
        };

        let container_name = format!("simcity-test-{}", uuid::Uuid::new_v4());
        crate::register_guard(crate::CleanupTask::Docker(crate::DockerGuard {
            container_name: container_name.clone(),
        }));

        let mut docker_run_cmd = Command::new("docker");
        docker_run_cmd.args([
            "run",
            "--rm",
            "--name",
            &container_name,
            "-v",
            &format!("{}:/app", pwd_str),
            "-v",
            &format!("{}:/app/target/golden_updates", temp_dir_str),
            "-v",
            &format!("{}:/sccache", sccache_dir_str),
            "-v",
            &format!(
                "{}:/usr/local/cargo/registry/index",
                cargo_registry_index_str
            ),
            "-v",
            &format!(
                "{}:/usr/local/cargo/registry/cache",
                cargo_registry_cache_str
            ),
            "-v",
            &format!("{}:/usr/local/cargo/git/db", cargo_git_db_str),
            "-e",
            &format!("SPLUG_GOLDEN_HOST_PATH={}", temp_dir_str),
            "-e",
            "SCCACHE_DIR=/sccache",
            "-e",
            "RUSTC_WRAPPER=",
            "-e",
            "DISPLAY=:99",
            "-e",
            &format!("VK_ICD_FILENAMES={}", icd_path),
            "-e",
            "XDG_RUNTIME_DIR=/tmp",
            "-w",
            "/app",
            &self.image_name,
            "bash",
            "-c",
            &bash_cmd,
        ]);

        let status = ctx
            .shell
            .run(&mut docker_run_cmd)
            .context("Failed to run docker container")?;

        if !status.success() {
            bail!("{} failed in Docker", cmd.description());
        }

        // Process coverage reports if this was a coverage command
        if let XtaskCommand::Coverage {
            text_output_path,
            lcov_output_path,
            html_output_dir,
            open,
            ..
        } = cmd
        {
            for path in [text_output_path, lcov_output_path].into_iter().flatten() {
                let host_path = pwd.join("target").join(path);
                if host_path.exists() {
                    println!("  ✓ Coverage report generated: {}", host_path.display());
                } else {
                    println!(
                        "  ⚠ Coverage report generation requested but file not found: {}",
                        host_path.display()
                    );
                }
            }

            crate::process_coverage_reports(ctx, lcov_output_path, html_output_dir, "/app", *open)?;
        }

        println!("  ✓ {} passed (Docker)", cmd.description());
        Ok(true)
    }

    fn is_available(&self, shell: &dyn Shell) -> bool {
        use std::process::Command;

        let mut cmd = Command::new("docker");
        cmd.arg("--version");
        shell.output(&mut cmd).is_ok()
    }

    fn name(&self) -> &str {
        "Docker"
    }
}

/// TartVM execution environment.
///
/// Runs commands inside a local macOS virtual machine using Tart.
/// Handles syncing source code via rsync and executing commands via SSH.
#[allow(dead_code)]
pub struct TartVMEnvironment {
    vm_name: String,
}

#[allow(dead_code)]
impl TartVMEnvironment {
    pub fn new(vm_name: String) -> Self {
        Self { vm_name }
    }

    /// Sync source code to VM using rsync.
    ///
    /// Excludes `target`, `.git`, and other unnecessary directories to speed up sync.
    pub(crate) fn sync_source_to_vm(
        &self,
        ctx: &XtaskContext,
        ip: &str,
        remote_path: &str,
    ) -> Result<()> {
        use anyhow::{Context as _, bail};
        use std::process::Command;

        println!("  Syncing source code to VM (rsync)...");

        // Prepare remote directory
        let mut ssh_prepare_cmd = Command::new("sshpass");
        ssh_prepare_cmd.args([
            "-p",
            "admin",
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            &format!("admin@{}", ip),
            "if [ -L /tmp/project ]; then rm /tmp/project; fi && mkdir -p /tmp/project",
        ]);
        let _ = ctx.shell.run(&mut ssh_prepare_cmd).ok();

        // Create remote directory
        let mut mkdir_remote_cmd = Command::new("sshpass");
        mkdir_remote_cmd.args([
            "-p",
            "admin",
            "ssh",
            "-o",
            "StrictHostKeyChecking=no",
            &format!("admin@{}", ip),
            &format!("mkdir -p {}", remote_path),
        ]);
        let _ = ctx.shell.run(&mut mkdir_remote_cmd).ok();

        // Rsync source
        let rsync_args_base = [
            "-p",
            "admin",
            "rsync",
            "-avz",
            "--no-times",
            "--delete",
            "--exclude",
            ".git",
            "--exclude",
            "*.profraw",
            "--exclude",
            "target",
            "--exclude",
            "extern/*/target",
            "--exclude",
            "extern/*/.git",
            "--exclude",
            "node_modules",
            "-e",
            "ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null",
        ];

        let mut rsync_cmd = Command::new("sshpass");
        rsync_cmd.args(rsync_args_base).arg(".").arg(format!(
            "admin@{}:{}",
            ip,
            remote_path.replace("~/", "")
        ));

        let status = ctx
            .shell
            .run(&mut rsync_cmd)
            .context("Failed to run rsync source")?;
        if !status.success() {
            bail!("Rsync source failed");
        }

        println!("  ✓ Sync complete");
        Ok(())
    }

    /// Execute command remotely in VM via SSH.
    ///
    /// Sets up necessary environment variables (SCCACHE, PATH) and installs
    /// dependencies if missing.
    pub(crate) fn execute_remote_command(
        &self,
        ctx: &XtaskContext,
        cmd: &XtaskCommand,
        ip: &str,
        remote_path: &str,
        remote_project_dir: &str,
    ) -> Result<bool> {
        use anyhow::Context as _;
        use std::process::Command;

        println!("  Building and Running {} in VM...", cmd.description());

        let inner_cmd = cmd.to_remote_command(crate::TestMode::Tart);
        let requirements = cmd.requirements_bash();

        let remote_bash_cmd = format!(
            "export PATH=$HOME/.cargo/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:$PATH && \\
             {} && \\
             export SPLUG_WORKSPACE_ROOT=$HOME/{} && \\
             cd {} && \\
             if ! command -v sccache &> /dev/null; then \\
                echo 'sccache not found, installing from binary...'; \\
                ARCH=$(uname -m); \\
                if [ \\\"$ARCH\\\" = \\\"arm64\\\" ]; then \\
                    URL=\\\"https://github.com/mozilla/sccache/releases/download/v0.13.0/sccache-v0.13.0-aarch64-apple-darwin.tar.gz\\\"; \\
                else \\
                    URL=\\\"https://github.com/mozilla/sccache/releases/download/v0.13.0/sccache-v0.13.0-x86_64-apple-darwin.tar.gz\\\"; \\
                fi; \\
                curl -LsSf \\\"$URL\\\" | tar zxf -; \\
                mkdir -p $HOME/.cargo/bin; \\
                mv sccache-*/sccache $HOME/.cargo/bin/; \\
                rm -rf sccache-*; \\
             fi && \\
             mkdir -p ~/sccache && \\
             export SCCACHE_DIR=~/sccache && \\
             export RUSTC_WRAPPER=sccache && \\
             echo 'Using sccache' && \\
             {}",
            requirements, remote_project_dir, remote_path, inner_cmd
        );

        let mut ssh_run_cmd = Command::new("sshpass");
        ssh_run_cmd.args([
            "-p",
            "admin",
            "ssh",
            "-t",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            &format!("admin@{}", ip),
            &remote_bash_cmd,
        ]);
        let run_status = ctx
            .shell
            .run(&mut ssh_run_cmd)
            .context("Failed to run SSH command")?;

        Ok(run_status.success())
    }

    /// Sync artifacts back from VM.
    ///
    /// Retrieves `target/golden_updates` and generated artifacts from the VM.
    pub(crate) fn sync_artifacts_from_vm(
        &self,
        ctx: &XtaskContext,
        ip: &str,
        remote_path: &str,
    ) -> Result<()> {
        use std::process::Command;

        // Sync artifacts back
        println!("  Syncing artifacts back from VM...");
        let mut scp_goldens_cmd = Command::new("sshpass");
        scp_goldens_cmd.args([
            "-p",
            "admin",
            "scp",
            "-r",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "UserKnownHostsFile=/dev/null",
            &format!("admin@{}:{}/target/golden_updates", ip, remote_path),
            "target/",
        ]);
        if let Err(e) = ctx.shell.run(&mut scp_goldens_cmd) {
            println!("  ⚠ Failed to sync target/golden_updates from VM: {}", e);
        } else {
            println!("  ✓ target/golden_updates synced from VM");
        }

        // Sync golden files
        println!("  Syncing golden files back from VM...");
        let mut scp_source_goldens_cmd = Command::new("sshpass");
        scp_source_goldens_cmd.args([
            "-p",
            "admin",
            "scp",
            "-r",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "UserKnownHostsFile=/dev/null",
            &format!("admin@{}:{}/crates/test-e2e/goldens", ip, remote_path),
            "crates/test-e2e/",
        ]);
        if let Err(e) = ctx.shell.run(&mut scp_source_goldens_cmd) {
            println!("  ⚠ Failed to sync crates/test-e2e/goldens from VM: {}", e);
        } else {
            println!("  ✓ crates/test-e2e/goldens synced from VM");
        }

        let mut scp_gui_goldens_cmd = Command::new("sshpass");
        scp_gui_goldens_cmd.args([
            "-p",
            "admin",
            "scp",
            "-r",
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "UserKnownHostsFile=/dev/null",
            &format!("admin@{}:{}/crates/gui/tests/goldens", ip, remote_path),
            "crates/gui/tests/",
        ]);
        let _ = ctx.shell.run(&mut scp_gui_goldens_cmd).ok();

        Ok(())
    }

    #[allow(clippy::too_many_arguments)]
    pub(crate) fn sync_coverage_reports_from_vm(
        &self,
        ctx: &XtaskContext,
        ip: &str,
        remote_path: &str,
        text_output_path: &Option<String>,
        lcov_output_path: &Option<String>,
        html_output_dir: &Option<String>,
        open_report: bool,
    ) -> Result<()> {
        use std::process::Command;

        println!("  Syncing coverage reports back from VM...");
        for path in [text_output_path, lcov_output_path].into_iter().flatten() {
            let mut scp_coverage_cmd = Command::new("sshpass");
            scp_coverage_cmd.args([
                "-p",
                "admin",
                "scp",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                &format!("admin@{}:{}/target/{}", ip, remote_path, path),
                &format!("target/{}", path),
            ]);

            if ctx.shell.run(&mut scp_coverage_cmd).is_ok() {
                let host_path = std::env::current_dir()?.join("target").join(path);
                println!("  ✓ Coverage report synced: {}", host_path.display());
            } else {
                println!("  ⚠ Failed to sync coverage report {} from VM", path);
            }
        }

        if let Some(dir) = html_output_dir {
            let mut scp_html_cmd = Command::new("sshpass");
            scp_html_cmd.args([
                "-p",
                "admin",
                "scp",
                "-r",
                "-o",
                "StrictHostKeyChecking=no",
                "-o",
                "UserKnownHostsFile=/dev/null",
                &format!("admin@{}:{}/target/{}", ip, remote_path, dir),
                "target/",
            ]);

            if ctx.shell.run(&mut scp_html_cmd).is_ok() {
                println!("  ✓ HTML report synced: target/{}", dir);
            } else {
                println!("  ⚠ Failed to sync HTML report directory {} from VM", dir);
            }
        }

        crate::process_coverage_reports(
            ctx,
            lcov_output_path,
            html_output_dir,
            remote_path,
            open_report,
        )?;

        Ok(())
    }
}

impl ExecutionEnvironment for TartVMEnvironment {
    fn build(&self, ctx: &XtaskContext) -> Result<()> {
        use anyhow::Context as _;
        use std::process::Command;

        // Check if VM exists and start it if needed
        let mut tart_list_cmd = Command::new("tart");
        tart_list_cmd.arg("list");
        let list_output = ctx
            .shell
            .output(&mut tart_list_cmd)
            .context("Failed to run tart list")?;

        let list_stdout = String::from_utf8_lossy(&list_output.stdout);
        let already_running = list_stdout
            .lines()
            .any(|line| line.contains(&self.vm_name) && line.contains("running"));

        if !already_running {
            println!("  Starting VM...");
            let mut tart_run_cmd = Command::new("tart");
            tart_run_cmd.args(["run", "--no-graphics", &self.vm_name]);
            ctx.shell
                .spawn(&mut tart_run_cmd)
                .context("Failed to start VM")?;

            println!("  Waiting for VM to boot...");
            let _ = crate::get_vm_ip(ctx.shell, &self.vm_name)?;
            std::thread::sleep(std::time::Duration::from_secs(5));
        } else {
            println!("  ✓ VM is already running");
        }

        Ok(())
    }

    fn execute(&self, ctx: &XtaskContext, cmd: &XtaskCommand) -> Result<bool> {
        use uuid::Uuid;

        println!(
            "Running {} in Tart VM ('{}')...",
            cmd.description(),
            self.vm_name
        );
        println!("{}", cmd.to_remote_command(crate::TestMode::Tart));

        // Get VM IP
        let ip = crate::get_vm_ip(ctx.shell, &self.vm_name)?;
        println!("  ✓ VM IP: {}", ip);

        // Wait for SSH
        crate::wait_for_ssh(ctx.shell, &ip)?;

        let remote_project_dir = format!("project_{}", Uuid::new_v4());
        let remote_path = format!("/tmp/{}", remote_project_dir);

        // Register cleanup guard
        crate::register_ctrlc_handler();
        crate::register_guard(crate::CleanupTask::Vm(crate::VmProjectGuard {
            ip: ip.clone(),
            remote_path: remote_path.clone(),
        }));

        // Sync source to VM using helper
        self.sync_source_to_vm(ctx, &ip, &remote_path)?;

        // Run command in VM using helper
        let run_success =
            self.execute_remote_command(ctx, cmd, &ip, &remote_path, &remote_project_dir)?;

        // Sync artifacts back using helper
        self.sync_artifacts_from_vm(ctx, &ip, &remote_path)?;

        // Handle coverage reports
        if let XtaskCommand::Coverage {
            text_output_path,
            lcov_output_path,
            html_output_dir,
            open,
            ..
        } = cmd
        {
            self.sync_coverage_reports_from_vm(
                ctx,
                &ip,
                &remote_path,
                text_output_path,
                lcov_output_path,
                html_output_dir,
                *open,
            )?;
        }

        if !run_success {
            return Ok(false);
        }

        println!("  ✓ {} passed (Tart)", cmd.description());

        // Cleanup is handled by drop(guard) at end of function or on Ctrl-C

        Ok(true)
    }

    fn is_available(&self, shell: &dyn Shell) -> bool {
        use std::process::Command;

        let mut cmd = Command::new("tart");
        cmd.arg("--version");
        shell.output(&mut cmd).is_ok()
    }

    fn name(&self) -> &str {
        "TartVM"
    }
}

/// Mock execution environment for testing
#[cfg(test)]
#[allow(dead_code)]
pub struct MockEnvironment {
    commands_executed: std::sync::Arc<std::sync::Mutex<Vec<String>>>,
    should_succeed: bool,
    is_available: bool,
}

#[cfg(test)]
#[allow(dead_code)]
impl MockEnvironment {
    pub fn new() -> Self {
        Self {
            commands_executed: std::sync::Arc::new(std::sync::Mutex::new(Vec::new())),
            should_succeed: true,
            is_available: true,
        }
    }

    pub fn with_success(mut self, should_succeed: bool) -> Self {
        self.should_succeed = should_succeed;
        self
    }

    pub fn with_availability(mut self, is_available: bool) -> Self {
        self.is_available = is_available;
        self
    }

    pub fn commands(&self) -> Vec<String> {
        self.commands_executed.lock().unwrap().clone()
    }
}

#[cfg(test)]
impl ExecutionEnvironment for MockEnvironment {
    fn build(&self, _ctx: &XtaskContext) -> Result<()> {
        let mut commands = self.commands_executed.lock().unwrap();
        commands.push("build".to_string());
        Ok(())
    }

    fn execute(&self, _ctx: &XtaskContext, cmd: &XtaskCommand) -> Result<bool> {
        let mut commands = self.commands_executed.lock().unwrap();
        commands.push(format!("execute: {}", cmd.description()));
        Ok(self.should_succeed)
    }

    fn is_available(&self, _shell: &dyn Shell) -> bool {
        self.is_available
    }

    fn name(&self) -> &str {
        "Mock"
    }
}
