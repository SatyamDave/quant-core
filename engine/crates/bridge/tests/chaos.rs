//! Issue #44: an external kill switch that works even if the agent process
//! itself is stuck. `--kill-file` (protocol v1.2) is an out-of-band trigger
//! independent of stdin: something that can still touch the filesystem (an
//! operator, a supervisor) engages the switch, and the bridge observes it on
//! the next record/op it handles from *any* source -- not necessarily the
//! agent's own decision loop. Mirrors
//! `engine/crates/replay/tests/chaos.rs`'s
//! `kill_switch_from_another_thread_halts_within_one_second`
//! (`tests/chaos/README.md`).

mod support;

use std::fs;
use std::thread;
use std::time::{Duration, Instant};

use qc_core::{OrderType, Side};

#[test]
fn kill_file_halts_within_one_second_and_cancels_every_open_order() {
    let dir = tempfile_dir();
    let kill_file = dir.join("kill");

    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    engine.set_kill_file(kill_file.clone());
    assert!(engine.next_decision_request().is_some(), "book is synced");

    // A resting order, as if the agent placed it just before going silent.
    let ok = engine.submit_order_intent(
        "dr-1",
        Side::Buy,
        "0.001".parse().unwrap(),
        "99.8".parse().unwrap(),
        OrderType::Limit,
    );
    assert_eq!(ok["accepted"], true, "{ok:#}");
    assert_eq!(engine.status()["open_orders"].as_array().unwrap().len(), 1);

    // The "agent" never sends another op again (simulating it being stuck
    // or crashed). A separate thread stands in for an operator/supervisor
    // touching the kill file directly on disk -- no stdin line involved.
    let trigger = thread::spawn(move || {
        thread::sleep(Duration::from_millis(50));
        let touched_at = Instant::now();
        fs::write(&kill_file, b"").expect("operator can write the kill file");
        touched_at
    });

    // Something still alive keeps polling the bridge with harmless ops --
    // in production this is the gateway's own periodic `status`/`reconcile`
    // heartbeat (wave-2 lane B), independent of whatever the agent's own
    // decision loop is doing.
    let started = Instant::now();
    let observed = loop {
        let status = engine.status();
        if status["halted"] != serde_json::Value::Null {
            break Instant::now();
        }
        assert!(
            started.elapsed() < Duration::from_secs(10),
            "kill file was never observed"
        );
        thread::sleep(Duration::from_millis(1));
        // `status` alone can't discover the file (it takes `&self`); poll it
        // the same way the wire dispatch does on every op.
        engine.poll_kill_file();
    };
    let touched_at = trigger.join().unwrap();

    let latency = observed.duration_since(touched_at);
    println!("kill file observed {latency:?} after it was written");
    assert!(latency < Duration::from_secs(1));
    assert_eq!(engine.status()["halted"], "kill_switch");

    // Done when #2: open orders actually get cancelled once triggered.
    assert_eq!(
        engine.status()["open_orders"].as_array().unwrap().len(),
        0,
        "the resting order must be cancelled, not just left to expire"
    );

    // Done when #1 (continued): still refuses new orders afterward, and the
    // agent's continued silence changes nothing -- enforcement never needed
    // its cooperation.
    let after = engine.submit_order_intent(
        "dr-2",
        Side::Buy,
        "0.001".parse().unwrap(),
        "99.8".parse().unwrap(),
        OrderType::Limit,
    );
    assert_eq!(after["accepted"], false);
    assert_eq!(after["halted"], "kill_switch");
}

#[test]
fn kill_file_absent_by_default_changes_nothing() {
    // No `--kill-file` flag (the v1/v1.1 default): a stray file at some
    // unrelated path must never be treated as a trigger.
    let mut engine = support::engine_from(&support::small_book_csv(), 1);
    assert!(engine.next_decision_request().is_some());
    engine.poll_kill_file();
    assert_eq!(engine.status()["halted"], serde_json::Value::Null);
}

fn tempfile_dir() -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "qc-bridge-chaos-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_nanos()
    ));
    fs::create_dir_all(&dir).expect("temp dir for the kill file");
    dir
}
