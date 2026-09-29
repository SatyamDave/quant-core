//! `--follow` mode (protocol v1.2, issue #48/shadow lane): tails a recording
//! file that keeps growing instead of requiring it complete up front, so
//! `qc-bridge` can drive a shadow run against a live/simulated feed one
//! append at a time.
//!
//! Tail semantics: only complete, newline-terminated lines are ever parsed —
//! a line the writer is still in the middle of appending is held back until
//! the rest of it arrives, so a read never sees a torn line. Determinism
//! ("same final file -> same decisions", issue #48's test) falls out of
//! that: [`FollowReader`] hands [`crate::engine::BridgeEngine`] the exact
//! same records, in the exact same order, that loading the finished file all
//! at once would -- only *when* each one becomes visible differs, and
//! nothing in the engine's decision logic depends on wall-clock arrival
//! time, only on each record's own `ts_local`.
//!
//! Never busy-spins: a caller with nothing new to report sleeps
//! [`POLL_INTERVAL`] between checks. `--stop-after-idle-ms` is for tests
//! only (a bounded run needs a bounded end); a real shadow run omits it and
//! [`FollowReader::wait_for_more`] blocks, however long it takes, until the
//! feed produces another record.

use std::io::{Read as _, Seek as _, SeekFrom};
use std::path::PathBuf;
use std::time::{Duration, Instant};

use qc_gateway::record::{Record, parse_line};

/// How often a blocked [`FollowReader::wait_for_more`] rechecks the file.
/// Small enough that a real shadow run reacts promptly to new quotes, large
/// enough not to busy-spin a CPU core for weeks at a time.
pub const POLL_INTERVAL: Duration = Duration::from_millis(20);

/// Tails one recording file from byte offset 0, parsing whatever complete
/// lines are currently available on each call.
pub struct FollowReader {
    path: PathBuf,
    offset: u64,
    /// Bytes read past the last complete line, held until the rest arrives.
    pending: Vec<u8>,
    /// 1-based count of complete lines handed to the parser so far, for
    /// `ParseError`'s line numbers (matches `qc_gateway::record::parse`'s
    /// own numbering, since both start at line 1 and count every line the
    /// file contains, comments and blanks included).
    lines_seen: usize,
    /// `--stop-after-idle-ms`: tests only (see module docs). `None` means
    /// "never give up" -- the correct default for a real, continuous run.
    stop_after_idle: Option<Duration>,
    /// Set once `stop_after_idle` has been exceeded with nothing new; from
    /// then on `wait_for_more` returns empty immediately rather than
    /// blocking again, matching a plain (non-follow) recording's "exhausted"
    /// behaviour once end of file is reached.
    gave_up: bool,
}

impl FollowReader {
    #[must_use]
    pub fn new(path: PathBuf, stop_after_idle: Option<Duration>) -> Self {
        Self {
            path,
            offset: 0,
            pending: Vec::new(),
            lines_seen: 0,
            stop_after_idle,
            gave_up: false,
        }
    }

    /// Reads whatever new complete lines are available right now, without
    /// blocking. A missing file (the writer hasn't created it yet) is not an
    /// error here -- it looks exactly like "nothing new yet".
    ///
    /// # Errors
    /// A malformed line, or any I/O error other than "not found".
    fn read_available(&mut self) -> Result<Vec<Record>, String> {
        let mut file = match std::fs::File::open(&self.path) {
            Ok(f) => f,
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(e) => return Err(format!("{}: {e}", self.path.display())),
        };
        file.seek(SeekFrom::Start(self.offset))
            .map_err(|e| format!("{}: {e}", self.path.display()))?;
        let mut buf = Vec::new();
        file.read_to_end(&mut buf)
            .map_err(|e| format!("{}: {e}", self.path.display()))?;
        if buf.is_empty() {
            return Ok(Vec::new());
        }
        self.offset += buf.len() as u64;
        self.pending.extend_from_slice(&buf);

        let Some(last_newline) = self.pending.iter().rposition(|&b| b == b'\n') else {
            // Nothing complete yet; keep buffering (the writer is still mid-line).
            return Ok(Vec::new());
        };
        let remainder = self.pending.split_off(last_newline + 1);
        let complete = std::mem::replace(&mut self.pending, remainder);

        let mut records = Vec::new();
        for line in complete.split(|&b| b == b'\n') {
            // `split` on the final `\n` yields one trailing empty slice; every
            // other blank/comment line is a real line `parse_line` itself skips.
            if line.is_empty() {
                continue;
            }
            self.lines_seen += 1;
            let text = String::from_utf8_lossy(line);
            match parse_line(self.lines_seen, &text) {
                Ok(Some(record)) => records.push(record),
                Ok(None) => {}
                Err(e) => return Err(e.to_string()),
            }
        }
        Ok(records)
    }

    /// Blocks (poll + sleep, never busy-spin) until either new records show
    /// up or the feed is judged over -- idle longer than
    /// `--stop-after-idle-ms` (tests), or a previous call already gave up.
    /// An empty result means "over"; callers treat that exactly like
    /// reaching the end of a plain (non-follow) recording.
    ///
    /// # Errors
    /// Whatever [`Self::read_available`] returns.
    pub fn wait_for_more(&mut self) -> Result<Vec<Record>, String> {
        if self.gave_up {
            return Ok(Vec::new());
        }
        let started = Instant::now();
        loop {
            let records = self.read_available()?;
            if !records.is_empty() {
                return Ok(records);
            }
            if let Some(idle) = self.stop_after_idle {
                if started.elapsed() >= idle {
                    self.gave_up = true;
                    return Ok(Vec::new());
                }
            }
            std::thread::sleep(POLL_INTERVAL);
        }
    }
}

#[cfg(test)]
mod tests {
    use std::io::Write as _;

    use super::FollowReader;

    #[test]
    fn buffers_a_line_the_writer_has_not_finished_yet() {
        let dir = std::env::temp_dir().join(format!(
            "qc-bridge-feed-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("growing.csv");
        std::fs::write(&path, "").unwrap();
        let mut reader = FollowReader::new(path.clone(), None);

        // Nothing written yet.
        assert_eq!(reader.read_available().unwrap(), Vec::new());

        // A torn line (no trailing newline): must not be parsed yet.
        {
            let mut f = std::fs::OpenOptions::new()
                .append(true)
                .open(&path)
                .unwrap();
            write!(f, "S,1,10,1000,1000,100.0@1;99.9@1,100.2@1;100.3@1").unwrap();
        }
        assert_eq!(
            reader.read_available().unwrap(),
            Vec::new(),
            "a line with no trailing newline must be held back, not parsed"
        );

        // Completing that line, plus one full second line.
        {
            let mut f = std::fs::OpenOptions::new()
                .append(true)
                .open(&path)
                .unwrap();
            write!(f, "\nD,1,11,B,99.8,1,2000,2000\n").unwrap();
        }
        let records = reader.read_available().unwrap();
        assert_eq!(records.len(), 2, "both now-complete lines parse in order");

        // A comment/blank line contributes nothing, and doesn't wedge the reader.
        {
            let mut f = std::fs::OpenOptions::new()
                .append(true)
                .open(&path)
                .unwrap();
            write!(f, "# a comment\n\nD,1,12,B,99.7,1,3000,3000\n").unwrap();
        }
        let records = reader.read_available().unwrap();
        assert_eq!(
            records.len(),
            1,
            "comment/blank lines are skipped, not records"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn stop_after_idle_gives_up_once_and_stays_given_up() {
        let dir = std::env::temp_dir().join(format!(
            "qc-bridge-feed-idle-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("never-grows.csv");
        std::fs::write(&path, "").unwrap();
        let mut reader = FollowReader::new(path, Some(std::time::Duration::from_millis(50)));

        let started = std::time::Instant::now();
        assert_eq!(reader.wait_for_more().unwrap(), Vec::new());
        assert!(
            started.elapsed() >= std::time::Duration::from_millis(50),
            "must actually wait out the idle window, not give up immediately"
        );

        // A second call must return immediately (already given up), not wait again.
        let started2 = std::time::Instant::now();
        assert_eq!(reader.wait_for_more().unwrap(), Vec::new());
        assert!(
            started2.elapsed() < std::time::Duration::from_millis(20),
            "a reader that already gave up must not wait out the idle window again"
        );

        let _ = std::fs::remove_dir_all(&dir);
    }
}
