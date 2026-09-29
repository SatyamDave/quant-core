//! Root rule 7: replaying the same recording must reproduce identical
//! output, byte for byte. Runs the actual compiled binary twice (like `just
//! replay` runs `qc-replay` twice) over the committed sample day.

use std::io::Write as _;
use std::process::{Command, Stdio};

fn script() -> String {
    let mut lines = Vec::new();
    for i in 0..20 {
        lines.push(format!(
            r#"{{"v":1,"id":"dr{i}","op":"next_decision_request"}}"#
        ));
        if i % 3 == 0 {
            lines.push(format!(
                r#"{{"v":1,"id":"sub{i}","op":"submit_order_intent","intent":
                {{"request_id":"dr-{i}","instrument":"1","side":"buy","qty":"0.001",
                  "limit_price":"65000.7","time_in_force":"gtc","reason":"scripted"}}}}"#
            ));
        }
        if i % 5 == 0 {
            lines.push(format!(
                r#"{{"v":1,"id":"nt{i}","op":"no_trade","request_id":"dr-{i}","reason":"skip"}}"#
            ));
        }
    }
    lines.push(r#"{"v":1,"id":"last","op":"status"}"#.to_owned());
    lines.push(r#"{"v":1,"id":"end","op":"shutdown"}"#.to_owned());
    lines.join("\n") + "\n"
}

fn run_once(recording: &str, script: &str) -> Vec<u8> {
    let limits = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../config/limits/default.toml"
    );
    let mut child = Command::new(env!("CARGO_BIN_EXE_qc-bridge"))
        .arg(recording)
        .arg("--limits")
        .arg(limits)
        .arg("--decide-every")
        .arg("5")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("qc-bridge binary starts");
    child
        .stdin
        .take()
        .unwrap()
        .write_all(script.as_bytes())
        .unwrap();
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "qc-bridge exited non-zero: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    output.stdout
}

#[test]
fn two_runs_over_the_sample_day_give_identical_output_bytes() {
    let recording = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../../tests/replay/sample_day.csv"
    );
    let script = script();
    let first = run_once(recording, &script);
    let second = run_once(recording, &script);
    assert_eq!(
        first, second,
        "same recording and script must give byte-identical output"
    );
    assert!(!first.is_empty());
}
