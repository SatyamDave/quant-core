//! `--follow` (issue #48/shadow lane): a writer appends records to a file
//! while the real `qc-bridge` binary follows it -- decisions arrive as the
//! file grows, the run stops cleanly once the writer is done and the feed
//! has been idle past `--stop-after-idle-ms`, and replaying the finished
//! file through a plain (non-follow) bridge gives the exact same sequence
//! of decisions ("same final file -> same ledger", regardless of the timing
//! records became visible under).

use std::fmt::Write as _;
use std::io::{BufRead, BufReader, Write as _};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::time::Duration;

use serde_json::Value;

fn limits_path() -> String {
    concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../config/limits/default.toml"
    )
    .to_owned()
}

fn temp_dir(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "qc-bridge-follow-test-{name}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

/// One snapshot (seq 10) plus `n_deltas` alternating-price deltas (seq 11..),
/// each on its own line, timestamps strictly increasing. Matches
/// `tests/support/mod.rs`'s `small_book_csv` fixture's shape.
fn synthetic_lines(n_deltas: u64) -> Vec<String> {
    let mut lines = vec!["S,1,10,1000,1000,100.0@1;99.9@1,100.2@1;100.3@1".to_owned()];
    for i in 0..n_deltas {
        let seq = 11 + i;
        let ts = 2000 + i * 1000;
        // Alternates the resting bid price a cent at a time; always leaves a
        // valid, synced two-sided book behind.
        let price = 99.8 - f64::from(u32::try_from(i).unwrap_or(u32::MAX)) * 0.01;
        lines.push(format!("D,1,{seq},B,{price:.2},1,{ts},{ts}"));
    }
    lines
}

fn spawn_bridge(recording: &std::path::Path, extra: &[&str]) -> Child {
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_qc-bridge"));
    cmd.arg(recording)
        .arg("--limits")
        .arg(limits_path())
        .arg("--decide-every")
        .arg("2");
    for a in extra {
        cmd.arg(a);
    }
    cmd.stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("qc-bridge binary starts")
}

fn send(stdin: &mut ChildStdin, line: &str) {
    writeln!(stdin, "{line}").unwrap();
    stdin.flush().unwrap();
}

/// Sends one `next_decision_request` and blocks for its single-line reply.
fn ask_for_decision(stdin: &mut ChildStdin, out: &mut impl BufRead, id: &str) -> Value {
    send(
        stdin,
        &format!(r#"{{"v":1,"id":"{id}","op":"next_decision_request"}}"#),
    );
    let mut line = String::new();
    out.read_line(&mut line).expect("bridge stdout still open");
    serde_json::from_str(&line).expect("response is valid JSON")
}

#[test]
fn follow_tails_a_growing_file_and_replaying_the_final_file_matches() {
    let dir = temp_dir("main");
    let recording = dir.join("growing.csv");
    // Deliberately not created yet: `--follow` must tolerate the writer not
    // having started, the same way it tolerates a mid-write pause later.
    let lines = synthetic_lines(7); // 1 snapshot + 7 deltas -> 4 decisions at decide_every=2

    let mut child = spawn_bridge(&recording, &["--follow", "--stop-after-idle-ms", "400"]);
    let mut stdin = child.stdin.take().unwrap();
    let mut stdout = BufReader::new(child.stdout.take().unwrap());

    let writer_recording = recording.clone();
    let writer = std::thread::spawn(move || {
        // Chunk 1: the file is created here, mid-test, on purpose.
        std::fs::write(&writer_recording, format!("{}\n", lines[0])).unwrap();
        std::thread::sleep(Duration::from_millis(40));
        for chunk in lines[1..].chunks(2) {
            let mut f = std::fs::OpenOptions::new()
                .append(true)
                .open(&writer_recording)
                .unwrap();
            for line in chunk {
                writeln!(f, "{line}").unwrap();
            }
            drop(f);
            std::thread::sleep(Duration::from_millis(40));
        }
    });

    let mut follow_decisions: Vec<Value> = Vec::new();
    for i in 0..6 {
        let resp = ask_for_decision(&mut stdin, &mut stdout, &format!("dr{i}"));
        assert_eq!(resp["ok"], true, "response: {resp}");
        let dr = resp["decision_request"].clone();
        if dr.is_null() {
            break;
        }
        follow_decisions.push(dr);
    }
    writer.join().unwrap();

    assert_eq!(
        follow_decisions.len(),
        4,
        "1 snapshot + 7 deltas at decide_every=2 must yield exactly 4 decisions, got {follow_decisions:?}"
    );

    // The feed is now static (writer done) and idle; the *next* call must
    // still come back cleanly (null), proving `--stop-after-idle-ms` gives
    // up instead of hanging forever.
    let after_idle = ask_for_decision(&mut stdin, &mut stdout, "after-idle");
    assert_eq!(after_idle["ok"], true);
    assert!(
        after_idle["decision_request"].is_null(),
        "once idle past --stop-after-idle-ms with the writer done, the feed must report exhausted, got {after_idle}"
    );

    send(&mut stdin, r#"{"v":1,"id":"end","op":"shutdown"}"#);
    let status = child.wait().expect("bridge process exits");
    assert!(
        status.success(),
        "qc-bridge must exit cleanly, got {status:?}"
    );

    // Replay: the SAME finished file, through a plain (non-follow) bridge,
    // scripted end to end in one shot (no live timing involved at all).
    let mut script = String::new();
    for i in 0..5 {
        writeln!(
            script,
            r#"{{"v":1,"id":"r{i}","op":"next_decision_request"}}"#
        )
        .unwrap();
    }
    writeln!(script, r#"{{"v":1,"id":"end","op":"shutdown"}}"#).unwrap();

    let mut replay = spawn_bridge(&recording, &[]);
    replay
        .stdin
        .take()
        .unwrap()
        .write_all(script.as_bytes())
        .unwrap();
    let output = replay.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "replay bridge exited non-zero: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    let replay_decisions: Vec<Value> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| serde_json::from_str::<Value>(line).ok())
        .filter_map(|v| {
            let dr = v.get("decision_request")?.clone();
            (!dr.is_null()).then_some(dr)
        })
        .collect();

    assert_eq!(
        follow_decisions, replay_decisions,
        "same final file must produce the same decisions whether it was followed live or replayed whole"
    );

    let _ = std::fs::remove_dir_all(&dir);
}
