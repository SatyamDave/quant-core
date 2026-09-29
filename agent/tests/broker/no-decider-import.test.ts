// The agent can't reach the broker: proves no file under agent/src/decider/ imports anything
// from agent/src/broker/ (issue #35's "no path the agent's own tool list can reach", made a
// structural test rather than a claim -- same spirit as agent/CLAUDE.md's "the loop fetches
// next_decision_request itself; the model never pulls market data through a tool").
//
// Wave-2 review finding (gateway-integration, #84): wave 2 intentionally wires agent/src/loop.ts
// (and agent/src/bridge.ts, agent/src/cli.ts) to agent/src/broker/ -- that IS the fix for the
// "gateway isn't wired to the real bridge" gap, not a violation of this boundary. An earlier
// version of this test reacted to that by narrowing its scan to "files under src/decider/ that
// directly import src/broker/" -- which dropped the recorder side-door check entirely, weakened
// the import-specifier regex back to missing dynamic/side-effect imports, and never caught a
// decider file reaching the broker *indirectly* (e.g. by importing loop.ts, which imports
// broker/). This version is an ALLOWLIST instead of a deny-list: a file under src/decider/ may
// import (directly or transitively, following only relative specifiers) nothing but other
// src/decider/** files, src/types.ts, src/schema.ts, src/decimal.ts, src/paths.ts (the one
// additional shared utility decider/live.ts already legitimately needs), src/prompts/**, or a
// bare package specifier (node builtins, the Agent SDK, zod, ...). broker/, loop.ts, bridge.ts,
// cli.ts and recorder/ are all, by construction, unreachable from decider/ -- there is no need to
// special-list them.
import { existsSync, readdirSync, readFileSync, statSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const AGENT_SRC = path.resolve(HERE, "..", "..", "src");
const DECIDER_SRC = path.join(AGENT_SRC, "decider");

function walk(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    const stat = statSync(full);
    if (stat.isDirectory()) out.push(...walk(full));
    else if (entry.endsWith(".ts")) out.push(full);
  }
  return out;
}

// Matches `from "x"`, `require("x")`, dynamic `import("x")`, side-effect `import "x"`,
// and `export ... from "x"`: any of them can pull the broker into a module.
const IMPORT_SPECIFIER = /(?:\bfrom|\brequire\(|\bimport\(|\bimport)\s*['"]([^'"]+)['"]/g;

function importsBroker(file: string): string[] {
  const src = readFileSync(file, "utf8");
  const hits: string[] = [];
  for (const match of src.matchAll(IMPORT_SPECIFIER)) {
    const specifier = match[1]!;
    if (!specifier.startsWith(".")) continue; // only same-package relative imports can reach src/broker
    const resolved = path.resolve(path.dirname(file), specifier);
    if (resolved.includes(`${path.sep}src${path.sep}broker${path.sep}`) || resolved.endsWith(`${path.sep}src${path.sep}broker`)) {
      hits.push(specifier);
    }
  }
  return hits;
}

// ---- decider/ allowlist -----------------------------------------------------------------------

const ALLOWED_EXACT = new Set(
  ["types.ts", "schema.ts", "decimal.ts", "paths.ts"].map((f) => path.join(AGENT_SRC, f).replace(/\.ts$/, "")),
);
const ALLOWED_DIR_PREFIXES = [DECIDER_SRC, path.join(AGENT_SRC, "prompts")];

function stripExt(p: string): string {
  return p.replace(/\.(ts|js|mjs|cjs)$/, "");
}

/** Extracts every *relative* import specifier from source text (bare/package specifiers -- node
 *  builtins, the Agent SDK, zod, ... -- are always allowed and never even considered, same as
 *  `importsBroker` above: only a same-package relative import can reach another file in this
 *  tree). */
function relativeSpecifiers(source: string): string[] {
  return [...source.matchAll(IMPORT_SPECIFIER)].map((m) => m[1]!).filter((s) => s.startsWith("."));
}

function resolveSpecifier(fromFile: string, specifier: string): string {
  return stripExt(path.resolve(path.dirname(fromFile), specifier));
}

function isAllowedTarget(resolvedNoExt: string): boolean {
  if (ALLOWED_EXACT.has(resolvedNoExt)) return true;
  return ALLOWED_DIR_PREFIXES.some((dir) => resolvedNoExt === dir || resolvedNoExt.startsWith(dir + path.sep));
}

function resolveRealFile(resolvedNoExt: string): string | undefined {
  if (existsSync(resolvedNoExt) && statSync(resolvedNoExt).isFile()) return resolvedNoExt;
  const withTs = `${resolvedNoExt}.ts`;
  return existsSync(withTs) ? withTs : undefined;
}

/** Walks the import graph reachable (via relative specifiers only) from `entryFile` and returns
 *  one message per import that resolves outside the allowlist. Recurses into an allowed target
 *  outside src/decider/ too (types.ts/schema.ts/decimal.ts/paths.ts/prompts/**), so a decider file
 *  can't launder a disallowed import through one of those few shared files -- it also catches the
 *  case where one of them is later changed to import something it shouldn't. Targets inside
 *  src/decider/ itself are not re-walked here: the caller already scans every file under
 *  src/decider/ directly, so recursing into them here would be redundant, not incomplete. */
function disallowedImports(entryFile: string): string[] {
  const offenders: string[] = [];
  const visited = new Set<string>();
  function visit(file: string): void {
    if (visited.has(file)) return;
    visited.add(file);
    for (const specifier of relativeSpecifiers(readFileSync(file, "utf8"))) {
      const resolvedNoExt = resolveSpecifier(file, specifier);
      if (!isAllowedTarget(resolvedNoExt)) {
        offenders.push(
          `${path.relative(AGENT_SRC, entryFile)}: reaches disallowed import "${specifier}" (via ${path.relative(AGENT_SRC, file)})`,
        );
        continue;
      }
      if (!resolvedNoExt.startsWith(DECIDER_SRC)) {
        const real = resolveRealFile(resolvedNoExt);
        if (real) visit(real);
      }
    }
  }
  visit(entryFile);
  return offenders;
}

describe("agent/src/decider/ cannot reach agent/src/broker/, directly or transitively", () => {
  it("every relative import reachable from src/decider/ resolves inside the allowlist", () => {
    if (!existsSync(DECIDER_SRC)) return; // not built yet in this worktree; see header comment
    const files = walk(DECIDER_SRC);
    const offenders = files.flatMap((f) => disallowedImports(f));
    expect(offenders).toEqual([]);
  });

  it("nothing outside src/recorder imports src/recorder, so the recorder can't be a side door to the broker", () => {
    // The recorder legitimately imports the broker's read-only quote mapping (agent/CLAUDE.md).
    // That exception is only safe if no other code (decider, loop, bridge client) can reach the
    // broker through the recorder.
    const files = walk(AGENT_SRC).filter((f) => !f.includes(`${path.sep}src${path.sep}recorder${path.sep}`));
    const offenders = files.flatMap((f) => {
      const src = readFileSync(f, "utf8");
      return relativeSpecifiers(src)
        .filter((spec) => path.resolve(path.dirname(f), spec).includes(`${path.sep}src${path.sep}recorder`))
        .map((spec) => `${path.relative(AGENT_SRC, f)}: imports "${spec}"`);
    });
    expect(offenders).toEqual([]);
  });

  describe("sanity: the allowlist scanner actually catches each bypass", () => {
    // Resolution only depends on path arithmetic (dirname of the "from" file + the specifier),
    // so these fixtures never need to touch disk -- same convention the pre-existing sanity test
    // below already used.
    const decoyFile = path.join(DECIDER_SRC, "__decoy_import_fixture.ts");

    function offendersFor(source: string): string[] {
      return relativeSpecifiers(source)
        .map((specifier) => ({ specifier, resolved: resolveSpecifier(decoyFile, specifier) }))
        .filter(({ resolved }) => !isAllowedTarget(resolved))
        .map(({ specifier }) => specifier);
    }

    it("catches a direct broker import", () => {
      expect(offendersFor('import { BrokerGateway } from "../broker/gateway.js";\n')).toEqual(["../broker/gateway.js"]);
    });

    it("catches an import of loop.ts -- a transitive path to the broker, not just a direct one", () => {
      expect(offendersFor('import { runLoop } from "../loop.js";\n')).toEqual(["../loop.js"]);
    });

    it("catches a dynamic import() of the broker", () => {
      expect(offendersFor('const m = await import("../broker/gateway.js");\n')).toEqual(["../broker/gateway.js"]);
    });

    it("catches a side-effect import of the broker", () => {
      expect(offendersFor('import "../broker/gateway.js";\n')).toEqual(["../broker/gateway.js"]);
    });

    it("does not flag an allowed import", () => {
      expect(offendersFor('import { absFixed } from "../decimal.js";\nimport { repoRoot } from "../paths.js";\n')).toEqual([]);
    });
  });

  it("sanity: the legacy direct-import scanner still detects a broker import when one exists", () => {
    // Guards against importsBroker's regex/resolution silently matching nothing (e.g. after a
    // refactor) rather than because there's truly no import. Kept alongside the allowlist check
    // above since importsBroker also backs the recorder-side-door test.
    const decoy = path.join(HERE, "__decoy_import_fixture.ts");
    const fixtureSrc = 'import { BrokerGateway } from "../../src/broker/gateway.js";\n';
    const hits: string[] = [];
    for (const match of fixtureSrc.matchAll(IMPORT_SPECIFIER)) {
      const specifier = match[1]!;
      const resolved = path.resolve(path.dirname(decoy), specifier);
      if (resolved.includes(`${path.sep}src${path.sep}broker${path.sep}`) || resolved.endsWith(`${path.sep}src${path.sep}broker`)) {
        hits.push(specifier);
      }
    }
    expect(hits).toEqual(["../../src/broker/gateway.js"]);
  });
});
