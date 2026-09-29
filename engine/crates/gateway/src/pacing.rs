//! Venue rate limiting and reconnect backoff. Time is always passed in, never
//! read from the OS, so both are deterministic and testable.

use qc_core::Timestamp;

const NANOS_PER_MS: u64 = 1_000_000;

/// Token bucket: `capacity` requests at once, refilled at `per_sec` a second.
/// Tokens are counted in thousandths so refill needs no floats.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct TokenBucket {
    capacity_milli: u64,
    per_sec: u64,
    tokens_milli: u64,
    last: Timestamp,
}

impl TokenBucket {
    /// Starts full at `now`.
    #[must_use]
    pub fn new(capacity: u32, per_sec: u32, now: Timestamp) -> Self {
        let capacity_milli = u64::from(capacity) * 1000;
        Self {
            capacity_milli,
            per_sec: u64::from(per_sec),
            tokens_milli: capacity_milli,
            last: now,
        }
    }

    fn refill(&mut self, now: Timestamp) {
        let elapsed_ns = now.saturating_since(self.last);
        // per_sec tokens/s = per_sec milli-tokens/ms.
        let add = (elapsed_ns / NANOS_PER_MS).saturating_mul(self.per_sec);
        if add > 0 {
            self.tokens_milli = self
                .tokens_milli
                .saturating_add(add)
                .min(self.capacity_milli);
            self.last = Timestamp(self.last.0 + (elapsed_ns / NANOS_PER_MS) * NANOS_PER_MS);
        }
    }

    /// Takes one token, or says how many milliseconds until one is available.
    ///
    /// # Errors
    /// `Err(retry_after_ms)` when the bucket is empty (never 0).
    pub fn try_acquire(&mut self, now: Timestamp) -> Result<(), u64> {
        self.refill(now);
        if self.tokens_milli >= 1000 {
            self.tokens_milli -= 1000;
            return Ok(());
        }
        if self.per_sec == 0 {
            return Err(u64::MAX);
        }
        Err((1000 - self.tokens_milli).div_ceil(self.per_sec))
    }
}

/// Delay before reconnect attempt `attempt` (0-based): exponential from
/// `base_ms`, capped at `max_ms`, with "equal jitter" (half fixed, half
/// pseudo-random from `seed`) so many clients do not reconnect in lockstep.
/// The same inputs always give the same delay.
#[must_use]
pub fn reconnect_delay_ms(attempt: u32, base_ms: u64, max_ms: u64, seed: u64) -> u64 {
    let exp = base_ms
        .checked_shl(attempt.min(63))
        .filter(|v| v >> attempt.min(63) == base_ms)
        .unwrap_or(u64::MAX)
        .min(max_ms);
    let half = exp / 2;
    half + splitmix64(seed ^ u64::from(attempt)) % (exp - half + 1)
}

fn splitmix64(mut x: u64) -> u64 {
    x = x.wrapping_add(0x9E37_79B9_7F4A_7C15);
    x = (x ^ (x >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
    x = (x ^ (x >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
    x ^ (x >> 31)
}

#[cfg(test)]
mod tests {
    use super::*;

    const MS: u64 = 1_000_000;

    #[test]
    fn bucket_allows_a_burst_then_paces_and_refills() {
        let mut b = TokenBucket::new(2, 10, Timestamp(0));
        assert_eq!(b.try_acquire(Timestamp(0)), Ok(()));
        assert_eq!(b.try_acquire(Timestamp(0)), Ok(()));
        assert_eq!(b.try_acquire(Timestamp(0)), Err(100));
        assert_eq!(b.try_acquire(Timestamp(40 * MS)), Err(60));
        assert_eq!(b.try_acquire(Timestamp(100 * MS)), Ok(()));
        // A long idle only refills to capacity.
        let t = Timestamp(3_600_000 * MS);
        assert_eq!(b.try_acquire(t), Ok(()));
        assert_eq!(b.try_acquire(t), Ok(()));
        assert!(b.try_acquire(t).is_err());
        // Sub-millisecond steps are not lost.
        let mut b = TokenBucket::new(1, 1000, Timestamp(0));
        b.try_acquire(Timestamp(0)).unwrap();
        for i in 1..=3 {
            let _ = b.try_acquire(Timestamp(i * MS / 4));
        }
        assert_eq!(b.try_acquire(Timestamp(MS + MS / 4)), Ok(()));
        assert_eq!(
            TokenBucket::new(0, 0, Timestamp(0)).try_acquire(Timestamp(0)),
            Err(u64::MAX)
        );
    }

    #[test]
    fn backoff_grows_is_capped_jittered_and_deterministic() {
        for attempt in 0..100 {
            let d = reconnect_delay_ms(attempt, 100, 30_000, 7);
            let exp = (100_u64 << attempt.min(20)).min(30_000);
            assert!(d >= exp / 2 && d <= exp, "attempt {attempt}: {d}");
            assert_eq!(d, reconnect_delay_ms(attempt, 100, 30_000, 7));
        }
        assert!(reconnect_delay_ms(u32::MAX, u64::MAX, u64::MAX, 1) >= u64::MAX / 2);
        let seeds: std::collections::BTreeSet<_> = (0..20)
            .map(|s| reconnect_delay_ms(10, 100, 30_000, s))
            .collect();
        assert!(seeds.len() > 1, "jitter should differ across seeds");
    }
}
