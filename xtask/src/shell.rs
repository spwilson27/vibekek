use anyhow::{Context, Result};
#[cfg(test)]
use std::collections::VecDeque;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Output};
#[cfg(test)]
use std::sync::Mutex;

/// Trait for executing shell commands and performing filesystem operations.
/// This allows mocking for unit tests.
pub trait Shell {
    fn run(&self, cmd: &mut Command) -> Result<ExitStatus>;
    fn output(&self, cmd: &mut Command) -> Result<Output>;
    fn spawn(&self, cmd: &mut Command) -> Result<()>;
    fn current_dir(&self) -> Result<PathBuf>;
    fn create_dir_all(&self, path: &Path) -> Result<()>;
    #[allow(dead_code)]
    fn exists(&self, path: &Path) -> bool;
    #[allow(dead_code)]
    fn remove_file(&self, path: &Path) -> Result<()>;
    fn copy(&self, src: &Path, dst: &Path) -> Result<u64>;
    fn write(&self, path: &Path, contents: &str) -> Result<()>;
    fn read(&self, path: &Path) -> Result<String>;
    fn rename(&self, src: &Path, dst: &Path) -> Result<()>;
    fn remove_dir_all(&self, path: &Path) -> Result<()>;
}

/// Real implementation using std::process::Command and std::fs.
pub struct RealShell;

impl Shell for RealShell {
    fn run(&self, cmd: &mut Command) -> Result<ExitStatus> {
        let status = cmd
            .status()
            .context(format!("Failed to run command: {:?}", cmd))?;
        Ok(status)
    }

    fn output(&self, cmd: &mut Command) -> Result<Output> {
        let output = cmd
            .output()
            .context(format!("Failed to run command: {:?}", cmd))?;
        Ok(output)
    }

    fn spawn(&self, cmd: &mut Command) -> Result<()> {
        let _ = cmd
            .spawn()
            .context(format!("Failed to spawn command: {:?}", cmd))?;
        Ok(())
    }

    fn current_dir(&self) -> Result<PathBuf> {
        std::env::current_dir().context("Failed to get current directory")
    }

    fn create_dir_all(&self, path: &Path) -> Result<()> {
        std::fs::create_dir_all(path).context(format!("Failed to create directory: {:?}", path))
    }

    fn exists(&self, path: &Path) -> bool {
        path.exists()
    }

    fn remove_file(&self, path: &Path) -> Result<()> {
        std::fs::remove_file(path).context(format!("Failed to remove file: {:?}", path))
    }

    fn copy(&self, src: &Path, dst: &Path) -> Result<u64> {
        std::fs::copy(src, dst).context(format!("Failed to copy {:?} to {:?}", src, dst))
    }

    fn write(&self, path: &Path, contents: &str) -> Result<()> {
        std::fs::write(path, contents).context(format!("Failed to write to file: {:?}", path))
    }

    fn read(&self, path: &Path) -> Result<String> {
        std::fs::read_to_string(path).context(format!("Failed to read file: {:?}", path))
    }

    fn rename(&self, src: &Path, dst: &Path) -> Result<()> {
        std::fs::rename(src, dst).context(format!("Failed to rename {:?} to {:?}", src, dst))
    }

    fn remove_dir_all(&self, path: &Path) -> Result<()> {
        std::fs::remove_dir_all(path).context(format!("Failed to remove directory: {:?}", path))
    }
}

#[cfg(test)]
pub struct MockShell {
    pub recorded_commands: Mutex<Vec<String>>,
    pub output_queue: Mutex<VecDeque<Output>>,
    pub read_results: Mutex<VecDeque<Result<String, String>>>,
}

#[cfg(test)]
impl MockShell {
    pub fn new() -> Self {
        Self {
            recorded_commands: Mutex::new(Vec::new()),
            output_queue: Mutex::new(VecDeque::new()),
            read_results: Mutex::new(VecDeque::new()),
        }
    }

    #[allow(dead_code)]
    pub fn push_output(&self, stdout: &[u8], stderr: &[u8], success: bool) {
        #[cfg(unix)]
        let status = {
            use std::os::unix::process::ExitStatusExt;
            if success {
                ExitStatus::from_raw(0)
            } else {
                ExitStatus::from_raw(1)
            }
        };

        #[cfg(not(unix))]
        let status = {
            if success {
                // This is a hack for non-unix tests, but we mostly run on unix in CI/VM
                panic!("MockShell push_output only fully supported on unix for now")
            } else {
                panic!("MockShell push_output only fully supported on unix for now")
            }
        };

        let output = Output {
            status,
            stdout: stdout.to_vec(),
            stderr: stderr.to_vec(),
        };
        self.output_queue.lock().unwrap().push_back(output);
    }

    #[allow(dead_code)]
    pub fn push_read_result(&self, result: Result<String, String>) {
        self.read_results.lock().unwrap().push_back(result);
    }
}

#[cfg(test)]
impl Shell for MockShell {
    fn run(&self, cmd: &mut Command) -> Result<ExitStatus> {
        let cmd_str = format!("{:?}", cmd);
        self.recorded_commands.lock().unwrap().push(cmd_str);

        // Use queued output if available
        let mut queue = self.output_queue.lock().unwrap();
        if let Some(output) = queue.pop_front() {
            return Ok(output.status);
        }

        // Return success by default
        #[cfg(unix)]
        {
            use std::os::unix::process::ExitStatusExt;
            Ok(ExitStatus::from_raw(0))
        }
        #[cfg(windows)]
        {
            Ok(Command::new("cmd").args(["/c", "exit 0"]).status().unwrap())
        }
    }

    fn output(&self, cmd: &mut Command) -> Result<Output> {
        let cmd_str = format!("{:?}", cmd);
        self.recorded_commands.lock().unwrap().push(cmd_str);

        // Return next queued output or default empty success
        let mut queue = self.output_queue.lock().unwrap();
        if let Some(output) = queue.pop_front() {
            Ok(output)
        } else {
            #[cfg(unix)]
            {
                use std::os::unix::process::ExitStatusExt;
                Ok(Output {
                    status: ExitStatus::from_raw(0),
                    stdout: Vec::new(),
                    stderr: Vec::new(),
                })
            }
            #[cfg(windows)]
            {
                // Create a dummy output
                Ok(Output {
                    status: Command::new("cmd").args(["/c", "exit 0"]).status().unwrap(),
                    stdout: Vec::new(),
                    stderr: Vec::new(),
                })
            }
        }
    }

    fn spawn(&self, cmd: &mut Command) -> Result<()> {
        let cmd_str = format!("{:?}", cmd);
        self.recorded_commands.lock().unwrap().push(cmd_str);
        Ok(())
    }

    fn current_dir(&self) -> Result<PathBuf> {
        Ok(PathBuf::from("/mock/dir"))
    }

    fn create_dir_all(&self, path: &Path) -> Result<()> {
        self.recorded_commands
            .lock()
            .unwrap()
            .push(format!("create_dir_all {:?}", path));
        Ok(())
    }

    fn exists(&self, _path: &Path) -> bool {
        true
    }

    fn remove_file(&self, _path: &Path) -> Result<()> {
        Ok(())
    }

    fn copy(&self, src: &Path, dst: &Path) -> Result<u64> {
        self.recorded_commands
            .lock()
            .unwrap()
            .push(format!("copy {:?} -> {:?}", src, dst));
        Ok(0)
    }

    fn write(&self, path: &Path, _contents: &str) -> Result<()> {
        self.recorded_commands
            .lock()
            .unwrap()
            .push(format!("write {:?}", path));
        Ok(())
    }

    fn read(&self, path: &Path) -> Result<String> {
        let mut results = self.read_results.lock().unwrap();
        if let Some(res) = results.pop_front() {
            res.map_err(|e| anyhow::anyhow!(e))
        } else {
            anyhow::bail!("MockShell: No read result pushed for {:?}", path)
        }
    }

    fn rename(&self, src: &Path, dst: &Path) -> Result<()> {
        self.recorded_commands
            .lock()
            .unwrap()
            .push(format!("rename {:?} -> {:?}", src, dst));
        Ok(())
    }

    fn remove_dir_all(&self, path: &Path) -> Result<()> {
        self.recorded_commands
            .lock()
            .unwrap()
            .push(format!("remove_dir_all {:?}", path));
        Ok(())
    }
}

#[cfg(test)]
impl Default for MockShell {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod integration_tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_real_shell_integration() {
        let shell = RealShell;
        let dir = tempdir().unwrap();
        let path = dir.path().join("test_file.txt");

        // 1. Write
        shell.write(&path, "hello world").unwrap();
        assert!(path.exists());

        // 2. Exists
        assert!(shell.exists(&path));

        // 3. Current Dir
        let current = shell.current_dir().unwrap();
        assert!(current.exists());

        // 4. Copy
        let copy_path = dir.path().join("copy.txt");
        shell.copy(&path, &copy_path).unwrap();
        assert!(copy_path.exists());
        let content = std::fs::read_to_string(&copy_path).unwrap();
        assert_eq!(content, "hello world");

        // 5. Remove
        shell.remove_file(&path).unwrap();
        assert!(!path.exists());

        // 6. Run (echo)
        let mut cmd = Command::new("echo");
        cmd.arg("foo");
        let status = shell.run(&mut cmd).unwrap();
        assert!(status.success());

        // 7. Output (echo)
        let mut cmd = Command::new("echo");
        cmd.arg("bar");
        let output = shell.output(&mut cmd).unwrap();
        assert!(output.status.success());
        let stdout = String::from_utf8_lossy(&output.stdout);
        assert!(stdout.trim() == "bar");

        // 8. Create Dir All
        let sub = dir.path().join("sub/dir");
        shell.create_dir_all(&sub).unwrap();
        assert!(sub.exists());
    }
}
