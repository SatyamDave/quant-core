# config/venues

One TOML file per venue: endpoints, region, instrument list, fee tier, rate limits. Never credentials; keys come from the secret manager at runtime (root rule 2). Venue choice is recorded in ADR-0003 before a file lands here.
