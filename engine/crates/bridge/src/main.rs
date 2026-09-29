//! `qc-bridge <recording> [--limits <path>] [--decide-every <n>] [--tick <decimal>]
//!            [--model <path> --model-sha256 <hex>] [--venue sim|external]
//!            [--kill-file <path>] [--instrument <path.toml>]
//!            [--follow [--stop-after-idle-ms <n>]]`
//!
//! Reads protocol v1 requests as JSON Lines on stdin, writes one JSON Lines
//! response per request on stdout, and exits after `shutdown` or end of
//! input. Stdout carries only protocol lines (root rule: money is never a
//! float, and every response is schema-checked in tests); logs go to stderr.
//!
//! `--follow` (issue #48/shadow lane): `<recording>` is a file that keeps
//! growing (a live/simulated recorder still writing to it) instead of a
//! finished file. `next_decision_request` blocks (poll + sleep, see
//! `feed.rs`) instead of returning "exhausted" the moment it runs out of
//! records that are visible *yet*. `--stop-after-idle-ms` bounds that wait
//! for tests; a real run omits it and follows forever.

use std::io::{BufRead, Write as _};
use std::path::{Path, PathBuf};
use std::process::ExitCode;
use std::time::Duration;

use serde_json::Value;

use qc_bridge::engine::{BridgeEngine, LoadedModel, VenueMode};
use qc_bridge::feed::FollowReader;
use qc_bridge::instrument::Instrument;
use qc_inference::linear::LinearModel;
use qc_risk::{KillSwitch, Limits};

/// How often the watcher thread below checks `--kill-file`. Well inside the
/// 250 ms budget issue #44's reopened gap asks for, with headroom for OS
/// scheduling jitter.
const KILL_FILE_WATCH_INTERVAL: Duration = Duration::from_millis(50);

/// Issue #44 reopened: "a hung (alive but stuck) agent loop never calls
/// [the bridge]". Every existing kill-file check
/// (`BridgeEngine::poll_kill_file`) runs on the *main* thread, inside
/// `process_record`/`handle_line` — so it only ever fires when something
/// sends this process a line on stdin. If nothing ever does (the agent
/// process is wedged, not merely slow, and its own heartbeat has somehow
/// also stopped), the main thread sits blocked in `stdin.lock().lines()`
/// forever and never gets a chance to notice the file.
///
/// This thread is the fix: it holds its own clone of the same
/// `Arc<AtomicBool>`-backed [`KillSwitch`] (confirmed at
/// `engine/crates/risk/src/lib.rs`'s `KillSwitch(Arc<AtomicBool>)`, already
/// `Clone`) and polls the file directly, independent of stdin, engaging the
/// switch itself the moment the file appears. `KillSwitch::engage` is a
/// plain atomic store — safe to call from any thread without touching the
/// engine, which the main thread alone owns.
///
/// ponytail: `eventlog::kill_switch_engaged`'s `ts_ns` is documented
/// everywhere else in this crate as "always the bridge's own market/sim
/// clock, never a wall-clock read" — but that clock lives inside
/// `BridgeEngine`, owned solely by the main thread, and is exactly what this
/// thread must not wait on. This one event, from this one thread, uses a
/// real wall-clock read instead; every other event this crate logs is
/// unaffected. If a caller ever needs this event's `ts_ns` in the same time
/// domain as the rest of the log, thread the engine's clock out through a
/// second `Arc` instead of reading `SystemTime::now()` here.
fn spawn_kill_file_watcher(kill_switch: KillSwitch, path: PathBuf) {
    std::thread::spawn(move || {
        loop {
            if path.exists() && !kill_switch.is_engaged() {
                kill_switch.engage();
                let ts_ns = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map_or(0, |d| u64::try_from(d.as_nanos()).unwrap_or(u64::MAX));
                qc_bridge::eventlog::kill_switch_engaged(ts_ns, "file");
            }
            std::thread::sleep(KILL_FILE_WATCH_INTERVAL);
        }
    });
}

struct Args {
    recording: String,
    limits: String,
    decide_every: u32,
    tick: String,
    model: Option<String>,
    model_sha256: Option<String>,
    venue: VenueMode,
    kill_file: Option<PathBuf>,
    instrument: Option<String>,
    follow: bool,
    stop_after_idle_ms: Option<u64>,
}

const DEFAULT_LIMITS: &str = "../config/limits/default.toml";
const DEFAULT_DECIDE_EVERY: u32 = 50;
const DEFAULT_TICK: &str = "0.1";

fn parse_args(mut rest: impl Iterator<Item = String>) -> Result<Args, String> {
    let recording = rest.next().ok_or("missing <recording>")?;
    let mut args = Args {
        recording,
        limits: DEFAULT_LIMITS.to_owned(),
        decide_every: DEFAULT_DECIDE_EVERY,
        tick: DEFAULT_TICK.to_owned(),
        model: None,
        model_sha256: None,
        venue: VenueMode::Sim,
        kill_file: None,
        instrument: None,
        follow: false,
        stop_after_idle_ms: None,
    };
    while let Some(flag) = rest.next() {
        let mut value = || rest.next().ok_or(format!("{flag} needs a value"));
        match flag.as_str() {
            "--limits" => args.limits = value()?,
            "--decide-every" => {
                args.decide_every = value()?
                    .parse()
                    .map_err(|_| "--decide-every must be a positive integer".to_owned())?;
            }
            "--tick" => args.tick = value()?,
            "--model" => args.model = Some(value()?),
            "--model-sha256" => args.model_sha256 = Some(value()?),
            "--venue" => args.venue = value()?.parse()?,
            "--kill-file" => args.kill_file = Some(PathBuf::from(value()?)),
            "--instrument" => args.instrument = Some(value()?),
            "--follow" => args.follow = true,
            "--stop-after-idle-ms" => {
                args.stop_after_idle_ms = Some(value()?.parse().map_err(|_| {
                    "--stop-after-idle-ms must be a non-negative integer".to_owned()
                })?);
            }
            other => return Err(format!("unknown flag {other}")),
        }
    }
    if args.stop_after_idle_ms.is_some() && !args.follow {
        return Err("--stop-after-idle-ms requires --follow".to_owned());
    }
    Ok(args)
}

fn load_model(args: &Args) -> Result<Option<LoadedModel>, Box<dyn std::error::Error>> {
    let (Some(path), Some(sha256)) = (&args.model, &args.model_sha256) else {
        return Ok(None);
    };
    let bytes = std::fs::read(path)?;
    let model = LinearModel::load(&bytes, sha256)?;
    Ok(Some(LoadedModel {
        model: Box::new(model),
        sha256: sha256.trim().to_owned(),
    }))
}

/// `--instrument <path.toml>` (protocol v1.2, issue #66): its own `limits`
/// field names the per-instrument limits file to load, in place of
/// `--limits` (which keeps its v1 meaning -- the global default -- only when
/// no instrument is configured). Fails closed on a config the bridge cannot
/// fully parse; never falls back to the hard-coded default silently.
fn load_instrument(path: &str) -> Result<Instrument, Box<dyn std::error::Error>> {
    let text = std::fs::read_to_string(path)?;
    let dir = Path::new(path).parent().unwrap_or_else(|| Path::new("."));
    Instrument::parse(&text, dir).map_err(Into::into)
}

/// Whether `line` (a raw wire request) is a `next_decision_request` op —
/// the only op `--follow` ever blocks a response for.
fn is_next_decision_request(line: &str) -> bool {
    serde_json::from_str::<Value>(line)
        .ok()
        .and_then(|v| v.get("op").and_then(Value::as_str).map(str::to_owned))
        .as_deref()
        == Some("next_decision_request")
}

/// Whether `response` (already dispatched) is a successful
/// `next_decision_request` reply that came back empty -- "not ready yet",
/// as opposed to a protocol error (missing `id`, bad JSON), which must never
/// be retried since retrying it would just reproduce the same error forever.
fn is_pending_decision(response: &str) -> bool {
    let Ok(v) = serde_json::from_str::<Value>(response) else {
        return false;
    };
    v.get("ok").and_then(Value::as_bool) == Some(true)
        && v.get("decision_request").is_none_or(Value::is_null)
}

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let args = parse_args(std::env::args().skip(1)).map_err(|e| {
        format!(
            "{e}\nusage: qc-bridge <recording> [--limits <path>] [--decide-every <n>] \
             [--tick <decimal>] [--model <path> --model-sha256 <hex>] [--venue sim|external] \
             [--kill-file <path>] [--instrument <path.toml>] \
             [--follow [--stop-after-idle-ms <n>]]"
        )
    })?;
    let instrument = args
        .instrument
        .as_deref()
        .map(load_instrument)
        .transpose()?;
    let limits_path = instrument
        .as_ref()
        .map_or_else(|| PathBuf::from(&args.limits), |i| i.limits_path.clone());
    let limits: Limits = toml::from_str(&std::fs::read_to_string(&limits_path)?)?;
    let tick = args.tick.parse().map_err(|e| format!("bad --tick: {e}"))?;
    // `--follow`: `feed::FollowReader` owns reading `<recording>` from byte 0,
    // including whatever is already there -- so the engine starts empty and
    // is fed exclusively through `BridgeEngine::feed_records` below, instead
    // of the one-shot `load_recording` a finished file uses.
    let mut follow_reader = args.follow.then(|| {
        FollowReader::new(
            PathBuf::from(&args.recording),
            args.stop_after_idle_ms.map(Duration::from_millis),
        )
    });
    let records = if follow_reader.is_some() {
        Vec::new()
    } else {
        BridgeEngine::load_recording(&std::fs::read_to_string(&args.recording)?)?
    };
    let model = load_model(&args)?;
    let kill_switch = KillSwitch::new();
    // Issue #44 reopened: a clone handed to a dedicated watcher thread, spawned
    // before the engine (which owns the other clone) ever starts reading
    // stdin — see spawn_kill_file_watcher's doc comment for why this can't
    // simply be BridgeEngine::poll_kill_file running more often on the main
    // thread.
    if let Some(path) = &args.kill_file {
        spawn_kill_file_watcher(kill_switch.clone(), path.clone());
    }
    let mut engine = BridgeEngine::with_instrument(
        records,
        limits,
        kill_switch,
        tick,
        args.decide_every,
        model,
        args.venue,
        args.kill_file,
        instrument,
    );

    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = stdout.lock();
    for line in stdin.lock().lines() {
        let line = line?;
        if line.trim().is_empty() {
            continue;
        }
        let (mut response, mut shutdown) = qc_bridge::handle_line(&mut engine, &line);
        // `--follow`: a `next_decision_request` that came back empty means
        // "nothing visible yet", not "feed exhausted" -- block for more
        // (feed.rs, never busy-spins) and retry the same request until a
        // decision arrives or the feed is judged over (idle timeout, tests
        // only; a real run has none and blocks as long as it takes).
        if let Some(reader) = follow_reader.as_mut() {
            if is_next_decision_request(&line) {
                while is_pending_decision(&response) {
                    let more = reader
                        .wait_for_more()
                        .map_err(|e| format!("--follow: {e}"))?;
                    if more.is_empty() {
                        break;
                    }
                    engine.feed_records(more);
                    let (r2, s2) = qc_bridge::handle_line(&mut engine, &line);
                    response = r2;
                    shutdown = s2;
                }
            }
        }
        writeln!(out, "{response}")?;
        out.flush()?;
        if shutdown {
            break;
        }
    }
    Ok(())
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("qc-bridge: {e}");
            ExitCode::FAILURE
        }
    }
}
