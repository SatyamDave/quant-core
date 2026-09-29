//! `qc-replay <recording> <order-log-out> [limits.toml]`: replays a recorded
//! day through the sample engine and writes the order log.

use std::process::ExitCode;

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let (recording, out, limits) = match args.as_slice() {
        [r, o] => (r, o, "../config/limits/default.toml"),
        [r, o, l] => (r, o, l.as_str()),
        _ => {
            eprintln!("usage: qc-replay <recording> <order-log-out> [limits.toml]");
            return ExitCode::from(2);
        }
    };
    let run = || -> Result<String, Box<dyn std::error::Error>> {
        let limits = toml::from_str(&std::fs::read_to_string(limits)?)?;
        let records = qc_gateway::record::parse(&std::fs::read_to_string(recording)?)?;
        let log = qc_replay::replay(&records, limits);
        std::fs::write(out, &log)?;
        Ok(log)
    };
    match run() {
        Ok(log) => {
            let count = |word: &str| {
                log.lines()
                    .filter(|l| l.split(' ').nth(1) == Some(word))
                    .count()
            };
            println!(
                "replayed {recording}: {} lines, {} submits, {} risk rejects, {} halts",
                log.lines().count(),
                count("submit"),
                count("risk-reject"),
                count("halt")
            );
            ExitCode::SUCCESS
        }
        Err(e) => {
            eprintln!("replay failed: {e}");
            ExitCode::FAILURE
        }
    }
}
