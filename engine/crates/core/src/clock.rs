//! Time comes from a [`Clock`] so replay can drive it deterministically.

/// Nanoseconds since the Unix epoch.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Default)]
pub struct Timestamp(pub u64);

impl Timestamp {
    /// Nanoseconds from `earlier` to `self`, or zero if `earlier` is later.
    #[must_use]
    pub fn saturating_since(self, earlier: Timestamp) -> u64 {
        self.0.saturating_sub(earlier.0)
    }
}

/// Engine code asks this for the time instead of calling the OS.
pub trait Clock {
    fn now(&self) -> Timestamp;
}

/// A clock that only moves when told to; used by replay, backtests and tests.
#[derive(Debug, Default)]
pub struct SimClock {
    now: Timestamp,
}

impl SimClock {
    #[must_use]
    pub fn new(start: Timestamp) -> Self {
        Self { now: start }
    }

    /// Moves time forward. Time never goes backwards, so an earlier value is ignored.
    pub fn advance_to(&mut self, t: Timestamp) {
        self.now = self.now.max(t);
    }
}

impl Clock for SimClock {
    fn now(&self) -> Timestamp {
        self.now
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sim_clock_is_monotonic() {
        let mut clock = SimClock::new(Timestamp(100));
        clock.advance_to(Timestamp(250));
        clock.advance_to(Timestamp(200));
        assert_eq!(clock.now(), Timestamp(250));
    }
}
