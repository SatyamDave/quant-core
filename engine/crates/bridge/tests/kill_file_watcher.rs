//! Issue #44 reopened: "a hung (alive but stuck) agent loop never calls
//! [the bridge]". `tests/chaos.rs`'s existing coverage
//! (`kill_file_halts_within_one_second_and_cancels_every_open_order`) proves
//! `BridgeEngine::poll_kill_file` works when *something* keeps calling it --
//! its own test loop does so directly, on the same thread, every millisecond.
//! It does not prove the real compiled binary observes the file with **zero**
//! stdin traffic at all, which is exactly the gap: before this test existed,
//! nothing did.
//!
//! This spawns the real `qc-bridge` binary, never writes a single byte to its
//! stdin, and proves the kill file is still observed (a `kill_switch_engaged`
//! line on stderr) within the reopened issue's budget -- proof that
//! `main.rs`'s watcher thread, not anything driven by stdin, is what
//! detected it.
use std::io::{BufRead, BufReader};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

#[test]
fn kill_file_is_observed_with_zero_stdin_traffic() {
    let bin = env!("CARGO_BIN_EXE_qc-bridge");
    let repo_root = concat!(env!("CARGO_MANIFEST_DIR"), "/../../..");
    let limits = format!("{repo_root}/config/limits/default.toml");

    let dir = std::env::temp_dir().join(format!(
        "qc-bridge-watcher-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    let kill_file = dir.join("kill");
    // An empty recording: the engine never has a record to process, so
    // `process_record` (and the `poll_kill_file` call inside it) never runs
    // either -- the only thing that can possibly observe the file is the
    // watcher thread.
    let recording = dir.join("empty.csv");
    std::fs::write(&recording, "").unwrap();

    let mut child = Command::new(bin)
        .arg(&recording)
        .arg("--limits")
        .arg(&limits)
        .arg("--kill-file")
        .arg(&kill_file)
        .stdin(Stdio::piped()) // held open, never written to, never closed
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn the real qc-bridge binary");

    let stderr = child.stderr.take().expect("stderr piped");
    let mut lines = BufReader::new(stderr).lines();

    // Give the process a moment to start and its watcher thread to begin
    // polling before the file even exists, so the very first poll after
    // creation is a realistic race, not a guaranteed hit on thread startup.
    std::thread::sleep(Duration::from_millis(100));
    let touched_at = Instant::now();
    std::fs::write(&kill_file, b"").expect("operator can write the kill file");

    let deadline = Instant::now() + Duration::from_secs(2);
    let mut latency = None;
    while Instant::now() < deadline {
        let Some(line) = lines.next() else { break };
        let Ok(line) = line else { break };
        if line.contains("\"event\":\"kill_switch_engaged\"") {
            latency = Some(touched_at.elapsed());
            break;
        }
    }

    let _ = child.kill();
    let _ = child.wait();

    let latency = latency.expect(
        "qc-bridge never logged kill_switch_engaged on stderr, with zero stdin traffic \
         -- the watcher thread did not observe the kill file independently",
    );
    println!("kill file observed (zero stdin traffic) after {latency:?}");
    assert!(
        latency < Duration::from_secs(1),
        "CLAUDE.md rule 11's one-second budget: took {latency:?}"
    );
}
