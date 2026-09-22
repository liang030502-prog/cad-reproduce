/**
 * Tests for the `/cad` host command.
 *
 * These are behavioural: they register the command against a stub host, then invoke the
 * handler the way the adapter does and read what comes back - the result text the GUI
 * would render, and the files the run is supposed to leave on disk.  A test that only
 * checked `register` was called would pass on a command that cannot run anything.
 *
 * Run:  node test/run.mjs
 */
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { apply, inject, name } from "../lib/index.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const PKG = resolve(HERE, "..");
const SKILL = process.env.DSH_CAD_SKILL ?? join(process.env.HOME ?? process.env.USERPROFILE ?? "", ".dsh", "skills", "cad-reproduce");
const FIXTURE = join(SKILL, "evals", "fixtures", "steelwork-1to20.pdf");

const failures = [];
function check(label, condition, detail = "") {
  const ok = Boolean(condition);
  console.log(`  [${ok ? "PASS" : "FAIL"}] ${label}${ok || !detail ? "" : `  -- ${detail}`}`);
  if (!ok) failures.push(`${label}: ${detail}`);
}

/** A host that records what was registered and can invoke it like the adapter does. */
function stubHost() {
  const registered = new Map();
  const ctx = {
    commands: {
      register(definition) {
        if (registered.has(definition.name)) throw new Error(`duplicate command ${definition.name}`);
        registered.set(definition.name, definition);
      },
    },
  };
  apply(ctx);
  const invoke = (line, attachments = []) => {
    const [verb] = line.trim().split(/\s+/u);
    const definition = registered.get("cad");
    if (!definition) throw new Error("command not registered");
    return definition.handler({ rawInput: line, attachments, agent: { id: "test-agent" }, verb });
  };
  return { registered, invoke };
}

function main() {
  console.log("dsh-command-cad tests");

  console.log("\n1. the plugin registers a host command");
  check("module exports name/inject/apply", name === "command-cad" && Array.isArray(inject) && typeof apply === "function",
        `${name} / ${JSON.stringify(inject)}`);
  check("it injects the command registry", inject.includes("commands"), JSON.stringify(inject));
  const host = stubHost();
  const def = host.registered.get("cad");
  check("it registered /cad", def !== undefined, [...host.registered.keys()].join(","));
  check("the name is a legal command name", /^[a-z0-9_-]+$/u.test(def?.name ?? ""), def?.name);
  check("it declares a description for the discovery menu", (def?.description ?? "").length > 10, def?.description);
  check("it accepts attachments, so a PDF can be dropped in",
        def?.input?.attachments === true, JSON.stringify(def?.input));
  check("it advertises its own syntax", (def?.input?.hint ?? "").includes("status"), def?.input?.hint);

  console.log("\n2. help and unknown subcommands answer without running anything");
  const help = host.invoke("help");
  check("help is a success result", help.kind === "success", help.kind);
  check("help documents every subcommand",
        ["run", "status", "test", "help"].every((v) => help.text.includes(v)), help.text.slice(0, 120));
  const bad = host.invoke("frobnicate");
  check("an unknown verb is an error result", bad.kind === "error", bad.kind);
  check("and it shows the usage", bad.text.includes("Usage: /cad"), bad.text.slice(0, 80));

  console.log("\n3. a missing source is refused before any stage runs");
  const empty = host.invoke("");
  check("no argument and no attachment is an error", empty.kind === "error", empty.kind);
  check("the error explains what is needed", empty.text.includes("no PDF given"), empty.text.slice(0, 80));
  const nowhere = host.invoke("run Z:/definitely/not/here.pdf");
  check("a path that does not exist is an error", nowhere.kind === "error", nowhere.kind);
  check("the error names the path", nowhere.text.includes("not/here.pdf") || nowhere.text.includes("not\\here.pdf"),
        nowhere.text.slice(0, 120));

  if (!existsSync(FIXTURE)) {
    console.log(`\n  SKIP  the real-drawing run: fixture not found at ${FIXTURE}`);
  } else {
    console.log("\n4. an end-to-end run over a real drawing");
    const work = mkdtempSync(join(tmpdir(), "cadcmd-"));
    const previous = process.env.DSH_CAD_WORKSPACE;
    process.env.DSH_CAD_WORKSPACE = work;
    try {
      const started = Date.now();
      const result = host.invoke(`run "${FIXTURE}"`);
      const seconds = (Date.now() - started) / 1000;
      console.log(`       ran in ${seconds.toFixed(1)}s -> ${result.kind}`);
      check("the run reports a result", result.kind === "success" || result.kind === "error", result.kind);
      check("the summary names the job directory", result.text.includes("job        :"), result.text.slice(0, 200));
      check("the summary reports the gate verdict", /self-check: (PASS|FAIL)/u.test(result.text), result.text.slice(0, 300));
      check("the summary reports geometric agreement", result.text.includes("geometric agreement :"), "");
      check("the summary reports the live dimension count", /dimensions : \d+/u.test(result.text), "");
      check("it names the DXF it wrote", result.text.includes("repro.dxf"), "");
      check("it names the difference image", result.text.includes("cmp_difference.png"), "");
      check("this sheet passes the gate", result.kind === "success",
            result.text.split("\n").filter((l) => l.includes("self-check")).join(" "));

      const jobsRoot = join(work, "jobs");
      const job = join(jobsRoot, "steelwork-1to20");
      for (const artefact of ["extract.json", "dims.json", "repro.dxf", "build.json", "cmp.json",
                              join("trace", "trace_source.png"), join("trace", "trace_generated.png"),
                              join("out", "cmp_difference.png")]) {
        check(`wrote ${artefact}`, existsSync(join(job, artefact)), join(job, artefact));
      }
      const cmp = JSON.parse(readFileSync(join(job, "cmp.json"), "utf8"));
      check("the gate report says what the summary says", cmp.pass === (result.kind === "success"),
            `${cmp.pass} vs ${result.kind}`);

      console.log("\n5. status reprints the newest job without re-running it");
      const status = host.invoke("status");
      check("status is a result", status.kind === "success" || status.kind === "error", status.kind);
      check("status found the job just run", status.text.includes("steelwork-1to20"), status.text.slice(0, 160));
      check("status repeats the gate verdict", /self-check: (PASS|FAIL)/u.test(status.text), "");
      check("status repeats the dimension count", /dimensions : \d+/u.test(status.text), "");
    } finally {
      if (previous === undefined) delete process.env.DSH_CAD_WORKSPACE;
      else process.env.DSH_CAD_WORKSPACE = previous;
      rmSync(work, { recursive: true, force: true });
    }
  }

  console.log("\n6. the bundle declares how it loads");
  const pkg = JSON.parse(readFileSync(join(PKG, "package.json"), "utf8"));
  check("package.json points at the module", pkg.main === "lib/index.js", pkg.main);
  check("it declares a dsh bundle patch", pkg.dsh?.bundle?.patch === "./cordis.patch.yml",
        JSON.stringify(pkg.dsh));
  check("the patch file exists", existsSync(join(PKG, "cordis.patch.yml")), "");
  const patch = readFileSync(join(PKG, "cordis.patch.yml"), "utf8");
  check("the patch inserts a row named after the package", patch.includes(`name: '${pkg.name}'`), patch);
  check("the row carries an id a user layer can target", patch.includes("id: command-cad"), patch);

  console.log();
  if (failures.length) {
    console.log(`FAILED: ${failures.length} check(s)`);
    for (const f of failures) console.log(`  - ${f}`);
    return 1;
  }
  console.log("all checks passed");
  return 0;
}

process.exit(main());
