use super::*;

use crate::shell::MockShell;
use crate::{Cli, Commands, XtaskContext, run_cli};
use tempfile::tempdir; // Access private helpers

#[test]
fn test_build_command() {
    let cli = Cli {
        command: Commands::Build(BuildArgs {
            release: false,
            docker: false,
        }),
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    run_cli(cli, &ctx).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("cargo") && cmd.contains("build"))
    );
}

#[test]
fn test_fix_runs_in_test_command() {
    let cli = Cli {
        command: Commands::Test(TestArgs {
            quick: false,
            passthrough: vec![],
            all: false,
            docker: true,
            vm: false,
            vm_name: "test-vm".to_string(),
            no_backtrace: false,
            full_backtrace: false,
        }),
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    run_cli(cli, &ctx).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("cargo") && cmd.contains("fix"))
    );
}

#[test]
fn test_docs_runs_in_test_command() {
    let cli = Cli {
        command: Commands::Test(TestArgs {
            quick: false,
            passthrough: vec![],
            all: false,
            docker: true,
            vm: false,
            vm_name: "test-vm".to_string(),
            no_backtrace: false,
            full_backtrace: false,
        }),
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    run_cli(cli, &ctx).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("cargo") && cmd.contains("doc"))
    );
}

#[test]
fn test_build_command_release() {
    let cli = Cli {
        command: Commands::Build(BuildArgs {
            release: true,
            docker: false,
        }),
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    run_cli(cli, &ctx).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("cargo") && cmd.contains("build") && cmd.contains("--release"))
    );
}

#[test]
fn test_build_command_docker() {
    let cli = Cli {
        command: Commands::Build(BuildArgs {
            release: false,
            docker: true,
        }),
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    run_cli(cli, &ctx).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("docker") && cmd.contains("build"))
    );
}

#[test]
fn test_lint_command() {
    let cli = Cli {
        command: Commands::Lint,
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    run_cli(cli, &ctx).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("cargo") && cmd.contains("clippy"))
    );
}

#[test]
fn test_test_command_docker() {
    let cli = Cli {
        command: Commands::Test(TestArgs {
            quick: true,
            docker: true,
            vm: false,
            vm_name: "test-vm".to_string(),
            all: false,
            no_backtrace: false,
            full_backtrace: false,
            passthrough: vec![],
        }),
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    // Provide output for any command calling .output()
    // run_command(Docker) calls:
    // 1. docker build (run)
    // 2. shell.current_dir() -> Ok("/mock/dir")
    // 3. create_dir_all (x3)
    // 4. docker run (run)

    // It seems no .output() calls in Docker path?
    // Wait, let's double check run_command(Docker).
    // It calls `shell.run`.
    // It calls `shell.current_dir`.

    let result = run_cli(cli, &ctx);
    assert!(result.is_ok());

    let commands = shell.recorded_commands.lock().unwrap();
    // Should see docker build
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("docker") && cmd.contains("build"))
    );
    // Should see docker run
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("docker") && cmd.contains("run"))
    );
}

#[test]
fn test_get_vm_ip_helper() {
    let shell = MockShell::new();
    // Simulate tart ip output
    shell.push_output(b"192.168.64.2\n", b"", true);

    let ip = get_vm_ip(&shell, "test-vm").unwrap();
    assert_eq!(ip, "192.168.64.2");
}

#[test]
fn test_wait_for_ssh_helper() {
    let shell = MockShell::new();
    // MockShell::run always returns success, so this should pass immediately
    wait_for_ssh(&shell, "1.2.3.4").unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(commands.iter().any(|cmd| cmd.contains("sshpass")));
}

#[test]
fn test_coverage_text_output_failure_handling() {
    let cli = Cli {
        command: Commands::Coverage(CoverageArgs {
            no_verify: false,
            no_backtrace: false,
            full_backtrace: false,
            all: false,
            docker: true, // Use Docker to trigger to_remote_command
            vm: false,
            vm_name: "test-vm".to_string(),
            text_output: true,
            open: false,
            passthrough: vec![],
        }),
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    // Run CLI
    let _ = run_cli(cli, &ctx);

    let commands = shell.recorded_commands.lock().unwrap();

    // Find the docker run command
    let docker_run = commands
        .iter()
        .find(|cmd| cmd.contains("docker") && cmd.contains("run") && cmd.contains("bash"))
        .expect("Should have run docker container");

    // We want to ensure it uses the pattern: cmd; EC=$?; report; exit $EC
    // NOT: cmd && report
    assert!(
        docker_run.contains("XT_EXIT=$?"),
        "Command should capture exit code: {}",
        docker_run
    );
    assert!(
        !docker_run.contains("&& cargo llvm-cov report"),
        "Command should not chain report with &&"
    );
}

#[test]
fn test_coverage_dual_output_generation() {
    let cli = Cli {
        command: Commands::Coverage(CoverageArgs {
            no_verify: false,
            no_backtrace: false,
            full_backtrace: false,
            all: false,
            docker: true,
            vm: false,
            vm_name: "test-vm".to_string(),
            text_output: true,
            open: true,
            passthrough: vec![],
        }),
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    let _ = run_cli(cli, &ctx);

    let commands = shell.recorded_commands.lock().unwrap();
    let docker_run = commands
        .iter()
        .find(|cmd| cmd.contains("docker") && cmd.contains("run") && cmd.contains("bash"))
        .expect("Should have run docker container");

    // Verify both reports are generated
    assert!(docker_run.contains("--text"), "Should generate text report");
    assert!(docker_run.contains("--lcov"), "Should generate LCOV report");

    // Check that reports are sequenced with ; and part of the same block
    assert!(
        docker_run.contains("XT_EXIT=$?"),
        "Reports should be sequenced correctly"
    );
}

#[test]
fn test_coverage_open_flag_generation() {
    let cli = Cli {
        command: Commands::Coverage(CoverageArgs {
            no_verify: false,
            no_backtrace: false,
            full_backtrace: false,
            all: false,
            docker: true,
            vm: false,
            vm_name: "test-vm".to_string(),
            text_output: false,
            open: true,
            passthrough: vec![],
        }),
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    let _ = run_cli(cli, &ctx);

    let commands = shell.recorded_commands.lock().unwrap();
    let docker_run = commands
        .iter()
        .find(|cmd| cmd.contains("docker") && cmd.contains("run") && cmd.contains("bash"))
        .expect("Should have run docker container");

    // Open should imply --lcov and --html
    assert!(
        docker_run.contains("--lcov"),
        "Open should trigger LCOV generation"
    );
    assert!(
        docker_run.contains("--html"),
        "Open should trigger HTML generation"
    );

    // Check that process_coverage_reports called (indirectly via genhtml mock in next steps if needed,
    // but here we just check if it compiles and runs correctly)
}

/// Long tests use long_test feature, so we disable default features for them.
/// This test verifies that the quick flag disables default features for the test
/// command.
#[test]
fn test_test_command_quick_disables_default_features() {
    let cli = Cli {
        command: Commands::Test(TestArgs {
            quick: true,
            docker: true,
            vm: false,
            vm_name: "test-vm".to_string(),
            all: false,
            no_backtrace: false,
            full_backtrace: false,
            passthrough: vec![],
        }),
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    run_cli(cli, &ctx).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    let docker_run = commands
        .iter()
        .find(|cmd| cmd.contains("docker") && cmd.contains("run") && cmd.contains("bash"))
        .expect("Should have run docker container");

    assert!(
        docker_run.contains("--no-default-features"),
        "Quick test should disable default features: {}",
        docker_run
    );
}

#[test]
fn test_get_modes_default() {
    let modes = get_modes(false, false, false, false);
    // Default should be Tart+Docker on macOS, or just Docker on Linux
    #[cfg(target_os = "macos")]
    {
        assert_eq!(modes.len(), 2);
        assert!(modes.contains(&TestMode::Tart));
        assert!(modes.contains(&TestMode::Docker));
    }

    #[cfg(target_os = "linux")]
    {
        assert_eq!(modes, vec![TestMode::Docker]);
    }
}

#[test]
fn test_get_modes_all() {
    let modes = get_modes(false, false, false, true);
    // All means Docker and Tart
    assert!(modes.contains(&TestMode::Docker));
    assert!(modes.contains(&TestMode::Tart));
    if std::env::var("CI").is_ok() {
        assert_eq!(modes.len(), 3);
        assert!(modes.contains(&TestMode::Native));
    } else {
        assert_eq!(modes.len(), 2);
        assert!(!modes.contains(&TestMode::Native));
    }
}

#[test]
fn test_get_modes_explicit_docker() {
    let modes = get_modes(false, true, false, false);
    assert_eq!(modes, vec![TestMode::Docker]);
}

#[test]
fn test_get_modes_explicit_vm() {
    let modes = get_modes(false, false, true, false);
    assert_eq!(modes, vec![TestMode::Tart]);
}

#[test]
fn test_get_modes_docker_and_vm() {
    let modes = get_modes(false, true, true, false);
    assert_eq!(modes.len(), 2);
    assert!(modes.contains(&TestMode::Docker));
    assert!(modes.contains(&TestMode::Tart));
}

// Tests for ExecutionEnvironment trait and DockerEnvironment
#[test]
fn test_docker_environment_build() {
    use crate::environment::{DockerEnvironment, ExecutionEnvironment};

    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);
    let env = DockerEnvironment::new();

    // Mock successful docker build
    shell.push_output(b"", b"", true);

    let result = env.build(&ctx);
    assert!(result.is_ok());

    // Verify docker build was called
    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("docker") && cmd.contains("build"))
    );
}

#[test]
fn test_docker_environment_build_failure() {
    use crate::environment::{DockerEnvironment, ExecutionEnvironment};

    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);
    let env = DockerEnvironment::new();

    // Mock failed docker build
    shell.push_output(b"", b"Build failed", false);

    let result = env.build(&ctx);
    assert!(result.is_err());
    assert!(
        result
            .unwrap_err()
            .to_string()
            .contains("Docker build failed")
    );
}

#[test]
fn test_docker_environment_execute_test_command() {
    use crate::environment::{DockerEnvironment, ExecutionEnvironment};

    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);
    let env = DockerEnvironment::new();

    let cmd = XtaskCommand::Test {
        no_backtrace: false,
        full_backtrace: false,
        no_default_features: false,
        passthrough: vec![],
    };

    // Mock successful docker run
    shell.push_output(b"", b"", true);

    let result = env.execute(&ctx, &cmd);
    assert!(result.is_ok());
    assert!(result.unwrap());

    // Verify docker run was called with correct image
    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("docker") && cmd.contains("run") && cmd.contains("simcity-ci"))
    );
}

#[test]
fn test_docker_environment_execute_coverage_command() {
    use crate::environment::{DockerEnvironment, ExecutionEnvironment};

    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);
    let env = DockerEnvironment::new();

    let cmd = XtaskCommand::Coverage {
        no_verify: false,
        no_backtrace: false,
        full_backtrace: false,
        passthrough: vec![],
        text_output_path: None,
        lcov_output_path: None,
        html_output_dir: None,
        open: false,
        host_root: None,
    };

    // Mock successful docker run
    shell.push_output(b"", b"", true);

    let result = env.execute(&ctx, &cmd);
    assert!(result.is_ok());
    assert!(result.unwrap());
}

#[test]
fn test_docker_environment_is_available() {
    use crate::environment::{DockerEnvironment, ExecutionEnvironment};

    let shell = MockShell::new();
    let env = DockerEnvironment::new();

    // Mock docker --version succeeds
    shell.push_output(b"Docker version 20.10.0", b"", true);

    assert!(env.is_available(&shell));
}

#[test]
fn test_mock_environment_basics() {
    use crate::environment::{ExecutionEnvironment, MockEnvironment};

    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);
    let env = MockEnvironment::new();

    // Test build
    assert!(env.build(&ctx).is_ok());
    assert_eq!(env.commands(), vec!["build"]);

    // Test execute
    let cmd = XtaskCommand::Test {
        no_backtrace: false,
        full_backtrace: false,
        no_default_features: false,
        passthrough: vec![],
    };
    assert!(env.execute(&ctx, &cmd).unwrap());
    assert_eq!(env.commands().len(), 2);
    assert!(env.commands()[1].contains("test"));
}

#[test]
fn test_mock_environment_failure() {
    use crate::environment::{ExecutionEnvironment, MockEnvironment};

    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);
    let env = MockEnvironment::new().with_success(false);

    let cmd = XtaskCommand::Test {
        no_backtrace: false,
        full_backtrace: false,
        no_default_features: false,
        passthrough: vec![],
    };

    assert!(!env.execute(&ctx, &cmd).unwrap());
}

#[test]
fn test_mock_environment_availability() {
    use crate::environment::{ExecutionEnvironment, MockEnvironment};

    let shell = MockShell::new();

    let available_env = MockEnvironment::new().with_availability(true);
    assert!(available_env.is_available(&shell));

    let unavailable_env = MockEnvironment::new().with_availability(false);
    assert!(!unavailable_env.is_available(&shell));
}

// Tests for TartVMEnvironment
#[test]
fn test_tartvm_environment_is_available() {
    use crate::environment::{ExecutionEnvironment, TartVMEnvironment};

    let shell = MockShell::new();
    let env = TartVMEnvironment::new("test-vm".to_string());

    // Mock tart --version succeeds
    shell.push_output(b"tart 2.0.0", b"", true);

    assert!(env.is_available(&shell));
}

#[test]
fn test_tartvm_environment_name() {
    use crate::environment::{ExecutionEnvironment, TartVMEnvironment};

    let env = TartVMEnvironment::new("test-vm".to_string());
    assert_eq!(env.name(), "TartVM");
}

#[test]
fn test_docker_environment_name() {
    use crate::environment::{DockerEnvironment, ExecutionEnvironment};

    let env = DockerEnvironment::new();
    assert_eq!(env.name(), "Docker");
}

#[test]
fn test_docker_environment_custom_image() {
    use crate::environment::{DockerEnvironment, ExecutionEnvironment};

    let env = DockerEnvironment::new().with_image_name("custom-image".to_string());
    // We can't easily test the image name since it's private, but we can create it
    assert_eq!(env.name(), "Docker");
}

#[test]
fn test_docker_environment_default() {
    use crate::environment::{DockerEnvironment, ExecutionEnvironment};

    let env = DockerEnvironment::default();
    assert_eq!(env.name(), "Docker");
}

#[test]
fn test_tartvm_sync_source_to_vm() {
    use crate::XtaskContext;
    use crate::environment::TartVMEnvironment;
    use crate::shell::MockShell;

    let shell = MockShell::new();
    let ctx = XtaskContext { shell: &shell };
    let env = TartVMEnvironment::new("test-vm".to_string());

    // Mock success for ssh_prepare (2 cmds), and rsync (1 cmd)
    shell.push_output(b"prepared", b"", true);
    shell.push_output(b"mkdir complete", b"", true);
    shell.push_output(b"rsync complete", b"", true);

    env.sync_source_to_vm(&ctx, "1.2.3.4", "/tmp/project")
        .unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|c| c.contains("ssh") && c.contains("mkdir -p /tmp/project"))
    );
    assert!(
        commands
            .iter()
            .any(|c| c.contains("rsync") && c.contains("1.2.3.4:/tmp/project"))
    );
}

#[test]
fn test_tartvm_execute_remote_command() {
    use crate::environment::TartVMEnvironment;
    use crate::shell::MockShell;
    use crate::{XtaskCommand, XtaskContext};

    let shell = MockShell::new();
    let ctx = XtaskContext { shell: &shell };
    let env = TartVMEnvironment::new("test-vm".to_string());
    let cmd = XtaskCommand::Test {
        no_backtrace: false,
        full_backtrace: false,
        no_default_features: false,
        passthrough: Vec::new(),
    };

    shell.push_output(b"success", b"", true);

    let result = env
        .execute_remote_command(&ctx, &cmd, "1.2.3.4", "/tmp/project", "proj_dir")
        .unwrap();
    assert!(result);

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|c| c.contains("ssh") && c.contains("cargo nextest run"))
    );
}

#[test]
fn test_tartvm_sync_artifacts_from_vm() {
    use crate::XtaskContext;
    use crate::environment::TartVMEnvironment;
    use crate::shell::MockShell;

    let shell = MockShell::new();
    let ctx = XtaskContext { shell: &shell };
    let env = TartVMEnvironment::new("test-vm".to_string());

    // Mock success for scp commands
    shell.push_output(b"goldens synced", b"", true);
    shell.push_output(b"source goldens synced", b"", true);
    shell.push_output(b"gui goldens synced", b"", true);

    env.sync_artifacts_from_vm(&ctx, "1.2.3.4", "/tmp/project")
        .unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|c| c.contains("scp") && (c.contains("golden_updates") || c.contains("goldens")))
    );
}

#[test]
fn test_tartvm_environment_build_already_running() {
    use crate::environment::{ExecutionEnvironment, TartVMEnvironment};

    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);
    let env = TartVMEnvironment::new("test-vm".to_string());

    // Mock tart list showing VM is already running
    shell.push_output(b"test-vm running (192.168.64.2)\n", b"", true);

    let result = env.build(&ctx);
    assert!(result.is_ok());

    let commands = shell.recorded_commands.lock().unwrap();
    // Should call tart list but NOT tart run
    assert!(
        commands
            .iter()
            .any(|c| c.contains("tart") && c.contains("list"))
    );
    assert!(
        !commands
            .iter()
            .any(|c| c.contains("tart") && c.contains("run"))
    );
}

#[test]
fn test_tartvm_environment_build_start_needed() {
    use crate::environment::{ExecutionEnvironment, TartVMEnvironment};

    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);
    let env = TartVMEnvironment::new("test-vm".to_string());

    // 1. tart list showing VM stopped
    shell.push_output(b"test-vm stopped\n", b"", true);
    // 2. get_vm_ip (called during wait for boot)
    shell.push_output(b"192.168.64.2\n", b"", true);

    let result = env.build(&ctx);
    assert!(result.is_ok());

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|c| c.contains("tart") && c.contains("list"))
    );
    assert!(
        commands
            .iter()
            .any(|c| c.contains("tart") && c.contains("run"))
    );
}

#[test]
fn test_tartvm_sync_coverage_reports_from_vm() {
    use crate::environment::TartVMEnvironment;

    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);
    let env = TartVMEnvironment::new("test-vm".to_string());

    let text_path = Some("coverage.txt".to_string());
    let lcov_path = Some("coverage.lcov".to_string());
    let html_dir = Some("coverage-html".to_string());

    // Mock scp calls (text, lcov, html)
    shell.push_output(b"text synced", b"", true);
    shell.push_output(b"lcov synced", b"", true);
    shell.push_output(b"html synced", b"", true);

    env.sync_coverage_reports_from_vm(
        &ctx,
        "1.2.3.4",
        "/tmp/remote",
        &text_path,
        &lcov_path,
        &html_dir,
        false,
    )
    .unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    // Check scp for text
    assert!(
        commands
            .iter()
            .any(|c| c.contains("scp") && c.contains("coverage.txt"))
    );
    // Check scp for lcov
    assert!(
        commands
            .iter()
            .any(|c| c.contains("scp") && c.contains("coverage.lcov"))
    );
    // Check scp for html dir (-r)
    assert!(
        commands
            .iter()
            .any(|c| c.contains("scp") && c.contains("-r") && c.contains("coverage-html"))
    );
}

#[test]
fn test_tartvm_environment_execute_integration() {
    use crate::environment::{ExecutionEnvironment, TartVMEnvironment};

    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);
    let env = TartVMEnvironment::new("test-vm".to_string());

    let cmd = XtaskCommand::Test {
        no_backtrace: false,
        full_backtrace: false,
        no_default_features: false,
        passthrough: vec![],
    };

    // Sequence of outputs needed:
    // 1. get_vm_ip
    shell.push_output(b"1.2.3.4\n", b"", true);
    // 2. wait_for_ssh (sshpass -p admin ssh ...)
    shell.push_output(b"ssh up", b"", true);
    // 3. sync_source_to_vm: ssh_prepare
    shell.push_output(b"prepared", b"", true);
    // 4. sync_source_to_vm: mkdir_remote
    shell.push_output(b"mkdir complete", b"", true);
    // 5. sync_source_to_vm: rsync
    shell.push_output(b"rsync complete", b"", true);
    // 6. execute_remote_command: ssh run
    shell.push_output(b"run complete", b"", true);
    // 7. sync_artifacts_from_vm: scp goldens
    shell.push_output(b"goldens complete", b"", true);
    // 8. sync_artifacts_from_vm: scp source goldens
    shell.push_output(b"source goldens complete", b"", true);
    // 9. sync_artifacts_from_vm: scp gui goldens
    shell.push_output(b"gui goldens complete", b"", true);

    let result = env.execute(&ctx, &cmd).unwrap();
    assert!(result);

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(commands.iter().any(|c| c.contains("rsync")));
    assert!(
        commands
            .iter()
            .any(|c| c.contains("ssh") && c.contains("cargo nextest run"))
    );
    assert!(commands.iter().any(|c| c.contains("scp")));
}

#[test]
fn test_tartvm_environment_execute_coverage_integration() {
    use crate::environment::{ExecutionEnvironment, TartVMEnvironment};

    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);
    let env = TartVMEnvironment::new("test-vm".to_string());

    let cmd = XtaskCommand::Coverage {
        no_verify: false,
        no_backtrace: false,
        full_backtrace: false,
        passthrough: vec![],
        text_output_path: Some("coverage.txt".to_string()),
        lcov_output_path: Some("coverage.lcov".to_string()),
        html_output_dir: Some("coverage-html".to_string()),
        open: false,
        host_root: None,
    };

    // Sequence for Coverage:
    // 1. get_vm_ip
    shell.push_output(b"1.2.3.4\n", b"", true);
    // 2. wait_for_ssh
    shell.push_output(b"ssh up", b"", true);
    // 3. sync_source_to_vm: ssh_prepare
    shell.push_output(b"prepared", b"", true);
    // 4. sync_source_to_vm: mkdir_remote
    shell.push_output(b"mkdir complete", b"", true);
    // 5. sync_source_to_vm: rsync
    shell.push_output(b"rsync complete", b"", true);
    // 6. execute_remote_command: ssh run
    shell.push_output(b"run complete", b"", true);
    // 7. sync_artifacts_from_vm: scp goldens
    shell.push_output(b"goldens complete", b"", true);
    // 8. sync_artifacts_from_vm: scp source goldens
    shell.push_output(b"source goldens complete", b"", true);
    // 9. sync_artifacts_from_vm: scp gui goldens
    shell.push_output(b"gui goldens complete", b"", true);
    // 10. sync_coverage_reports_from_vm: scp text
    shell.push_output(b"text synced", b"", true);
    // 11. sync_coverage_reports_from_vm: scp lcov
    shell.push_output(b"lcov synced", b"", true);
    // 12. sync_coverage_reports_from_vm: scp html
    shell.push_output(b"html synced", b"", true);

    let result = env.execute(&ctx, &cmd).unwrap();
    assert!(result);

    let commands = shell.recorded_commands.lock().unwrap();
    // Verify coverage report syncs were attempted
    assert!(
        commands
            .iter()
            .any(|c| c.contains("scp") && c.contains("coverage.txt"))
    );
    assert!(
        commands
            .iter()
            .any(|c| c.contains("scp") && c.contains("coverage.lcov"))
    );
    assert!(
        commands
            .iter()
            .any(|c| c.contains("scp") && c.contains("coverage-html"))
    );
}

#[test]
fn test_fix_asset_links_logic() {
    let content = r#"<link rel="stylesheet" href="style.css"><script src="control.js"></script>"#;
    let fixed = fix_asset_links(content, "style.css", "../");
    assert!(fixed.contains(r#"href="../style.css""#));

    let fixed2 = fix_asset_links(&fixed, "control.js", "../");
    assert!(fixed2.contains(r#"src="../control.js""#));
}

#[test]
fn test_fix_html_paths_logic() {
    let tmp = tempdir().unwrap();
    let html_file = tmp.path().join("test.html");
    let content = "<html><body>Source at /tmp/remote/path/file.rs</body></html>";
    std::fs::write(&html_file, content).unwrap();

    let shell = crate::shell::RealShell;
    fix_html_paths(
        &shell,
        &html_file,
        tmp.path(),
        "/tmp/remote/path",
        "/home/local/path",
    )
    .unwrap();

    let fixed_content = std::fs::read_to_string(&html_file).unwrap();
    assert!(fixed_content.contains("/home/local/path/file.rs"));
}

#[test]
fn test_process_coverage_reports_relocation() {
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    // Mock directory structure
    // host_dir = /mock/dir/target/coverage-html
    // html_content_dir = /mock/dir/target/coverage-html/html
    // coverage_dir = /mock/dir/target/coverage-html/html/coverage
    // remote_root = /remote/root
    // local_root = /mock/dir

    // Sequence of exists() calls:
    // 1. host_dir exists -> true
    // 2. host_dir/html exists -> true
    // 3. coverage_dir exists -> true
    // 4. source_dir exists -> true
    // 5. index.html exists -> true

    // Wait, exists() in MockShell returns true by default and doesn't consume queue.
    // renaming source_dir (coverage/remote/root) to target_dir (coverage/mock/dir)

    // We expect:
    // - create_dir_all for target_dir parent
    // - rename source_dir -> target_dir
    // - remove_dir_all for private and tmp

    let lcov = Some("coverage.lcov".to_string());
    let html = Some("coverage-html".to_string());

    // 1. Read LCOV report
    shell.push_read_result(Ok("SF:/remote/root/src/main.rs\nend_of_record".to_string()));

    process_coverage_reports(&ctx, &lcov, &html, "/remote/root", true).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();

    // Verify relocation commands
    assert!(
        commands
            .iter()
            .any(|c| c.contains("rename") && c.contains("remote/root"))
    );
    assert!(
        commands
            .iter()
            .any(|c| c.contains("remove_dir_all") && c.contains("private"))
    );

    // Verify open command
    assert!(
        commands
            .iter()
            .any(|c| c.contains("open") && c.contains("index.html"))
    );
}

#[test]
fn test_book_command_build() {
    let cli = Cli {
        command: Commands::Book {
            install: false,
            serve: false,
        },
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    // Mock mdbook --version check (succeeds)
    shell.push_output(b"mdbook v0.4.14", b"", true);
    // Mock mdbook build (succeeds)
    shell.push_output(b"", b"", true);

    run_cli(cli, &ctx).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    // Check version check
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("mdbook") && cmd.contains("--version"))
    );
    // Check build command
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("mdbook") && cmd.contains("build"))
    );
}

#[test]
fn test_book_command_install_and_serve() {
    let cli = Cli {
        command: Commands::Book {
            install: true,
            serve: true,
        },
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    // Sequence:
    // 1. cargo install mdbook (succeeds)
    shell.push_output(b"", b"", true);
    // 2. mdbook --version (succeeds)
    shell.push_output(b"mdbook v0.4.14", b"", true);
    // 3. mdbook serve (succeeds)
    shell.push_output(b"", b"", true);

    run_cli(cli, &ctx).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    // Check install
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("cargo") && cmd.contains("install") && cmd.contains("mdbook"))
    );
    // Check serve
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("mdbook") && cmd.contains("serve") && cmd.contains("--open"))
    );
}

#[test]
fn test_book_command_missing_mdbook_auto_install() {
    let cli = Cli {
        command: Commands::Book {
            install: false,
            serve: false,
        },
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    // Sequence:
    // 1. mdbook --version (fails)
    shell.push_output(b"", b"not found", false);
    // 2. Recursive calls with install=true:
    // 2a. cargo install mdbook (succeeds)
    shell.push_output(b"", b"", true);
    // 2b. mdbook --version (succeeds)
    shell.push_output(b"mdbook v0.4.14", b"", true);
    // 2c. mdbook build (succeeds)
    shell.push_output(b"", b"", true);

    run_cli(cli, &ctx).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    // Should see install command even though we didn't ask for it initially
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("cargo") && cmd.contains("install"))
    );
}

// TODO:Fix test so it doesn't actually try to use VM
//#[test]
//fn test_mutants_command_native() {
//    let cli = Cli {
//        command: Commands::Mutants(MutantsArgs {
//            native: true,
//            docker: false,
//            vm: false,
//            all: false,
//            vm_name: "test-vm".to_string(),
//            no_backtrace: false,
//            full_backtrace: false,
//            passthrough: vec![],
//        }),
//    };
//    let shell = MockShell::new();
//    let ctx = XtaskContext::new(&shell);
//
//    // Mock success for native command
//    shell.push_output(b"success", b"", true);
//
//    run_cli(cli, &ctx).unwrap();
//
//    let commands = shell.recorded_commands.lock().unwrap();
//    // Native should run bash -c with cargo mutants
//    assert!(
//        commands
//            .iter()
//            .any(|cmd| cmd.contains("bash") && cmd.contains("-c") && cmd.contains("cargo mutants"))
//    );
//}

#[test]
fn test_mutants_command_docker() {
    let cli = Cli {
        command: Commands::Mutants(MutantsArgs {
            native: false,
            docker: true,
            vm: false,
            all: false,
            vm_name: "test-vm".to_string(),
            no_backtrace: false,
            full_backtrace: false,
            passthrough: vec![],
        }),
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    // Mock success for docker build
    shell.push_output(b"", b"", true);
    // Mock success for docker run
    shell.push_output(b"", b"", true);

    run_cli(cli, &ctx).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("docker") && cmd.contains("build"))
    );
    assert!(
        commands.iter().any(|cmd| cmd.contains("docker")
            && cmd.contains("run")
            && cmd.contains("cargo mutants"))
    );
}

#[test]
fn test_mutants_command_vm() {
    let cli = Cli {
        command: Commands::Mutants(MutantsArgs {
            native: false,
            docker: false,
            vm: true,
            all: false,
            vm_name: "test-vm".to_string(),
            no_backtrace: false,
            full_backtrace: false,
            passthrough: vec![],
        }),
    };
    let shell = MockShell::new();
    let ctx = XtaskContext::new(&shell);

    // Sequence for VM:
    // 1. tart list (build)
    shell.push_output(b"test-vm running\n", b"", true);
    // 2. get_vm_ip
    shell.push_output(b"1.2.3.4\n", b"", true);
    // 3. wait_for_ssh
    shell.push_output(b"ssh up", b"", true);
    // 4. sync_source: ssh_prepare
    shell.push_output(b"", b"", true);
    // 5. sync_source: mkdir
    shell.push_output(b"", b"", true);
    // 6. sync_source: rsync
    shell.push_output(b"", b"", true);
    // 7. execute: ssh run
    shell.push_output(b"run complete", b"", true);
    // 8. sync_artifacts: scp 1
    shell.push_output(b"", b"", true);
    // 9. sync_artifacts: scp 2
    shell.push_output(b"", b"", true);
    // 10. sync_artifacts: scp 3
    shell.push_output(b"", b"", true);

    run_cli(cli, &ctx).unwrap();

    let commands = shell.recorded_commands.lock().unwrap();
    assert!(
        commands
            .iter()
            .any(|cmd| cmd.contains("ssh") && cmd.contains("cargo mutants"))
    );
}

#[test]
fn test_mutants_remote_command_generation() {
    let cmd = XtaskCommand::Mutants {
        no_backtrace: false,
        full_backtrace: true,
        passthrough: vec!["--jobs".to_string(), "4".to_string()],
    };

    let remote_cmd = cmd.to_remote_command(TestMode::Tart);
    assert!(remote_cmd.contains("cargo mutants --workspace"));
    assert!(remote_cmd.contains("RUST_BACKTRACE=full"));
    assert!(remote_cmd.contains("--jobs 4"));
    // Default --no-times when passthrough is empty is NOT here because passthrough is NOT empty
    assert!(!remote_cmd.contains("--no-times"));
}

#[test]
fn test_mutants_requirements_bash() {
    let cmd = XtaskCommand::Mutants {
        no_backtrace: false,
        full_backtrace: false,
        passthrough: vec![],
    };

    let reqs = cmd.requirements_bash();
    assert!(reqs.contains("command -v cargo-mutants"));
    assert!(reqs.contains("cargo install cargo-mutants"));
}
