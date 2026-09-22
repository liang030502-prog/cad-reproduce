/**
 * Tests for the `/cad` host command.
 *
 * These are behavioural: they register the command against a stub host, then invoke the
 * handler the way the adapter does and read what comes back - the result text the GUI
 * would render, and the files the run is supposed to leave on disk.  A test that only
 * checked `register` was called would pass on a command that cannot run anything.
 *
 * The dispatch cases exist because the first version treated the first word after `/cad`
 * as a verb, so a real user typing a sentence beside an attached PDF got
 * "unknown subcommand 完成并输出为pdf".  Every phrasing below is now a case.
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
const HOME = process.env.HOME ?? process.env.USERPROFILE ?? "";
const SKILL = process.env.DSH_CAD_SKILL ?? join(HOME, ".dsh", "skills", "cad-reproduce");
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
  apply({
    commands: {
      register(definition) {
        if (registered.has(definition.name)) throw new Error(`duplicate command ${definition.name}`);
        registered.set(definition.name, definition);
      },
    },
  });
  const definition = registered.get("cad");
  const invoke = (line, attachments = []) =>
    definition.handler({ rawInput: line, attachments, agent: { id: "test-agent" } });
  return { registered, definition, invoke };
}

/** An attachment shaped the way the host hands one over. */
const attachment = (path) => [{ type: "file", path, filename: path.split(/[\\/]/u).pop() }];

function main() {
  console.log("dsh-command-cad tests");

  console.log("\n1. the plugin registers a host command");
  check("module exports name/inject/apply",
        name === "command-cad" && Array.isArray(inject) && typeof apply === "function",
        `${name} / ${JSON.stringify(inject)}`);
  check("it injects the command registry", inject.includes("commands"), JSON.stringify(inject));
  const host = stubHost();
  const def = host.definition;
  check("it registered /cad", def !== undefined);
  check("the name is a legal command name", /^[a-z0-9_-]+$/u.test(def?.name ?? ""), def?.name);
  check("it declares a description for the discovery menu",
        (def?.description ?? "").length > 10, def?.description);
  check("it accepts attachments, so a PDF can be dropped in",
        def?.input?.attachments === true, JSON.stringify(def?.input));

  console.log("\n2. a sentence is not an unknown subcommand");
  // The exact input that produced the bug report.
  const prose = host.invoke("完成并输出为pdf");
  check("prose alone is NOT refused as an unknown subcommand",
        !prose.text.includes("unknown subcommand"), prose.text.slice(0, 90));
  check("prose alone asks for a PDF rather than claiming a bad verb",
        prose.text.includes("no PDF given"), prose.text.slice(0, 90));
  const proseWithAttachment = host.invoke("完成并输出为pdf", attachment(FIXTURE));
  check("prose beside an attachment runs on the attachment",
        !proseWithAttachment.text.includes("unknown subcommand")
        && proseWithAttachment.text.includes("steelwork-1to20"),
        proseWithAttachment.text.slice(0, 160));
  check("and the summary says where the source came from",
        /source\s+:.*attachment/u.test(proseWithAttachment.text),
        proseWithAttachment.text.split("\n").slice(0, 4).join(" | "));

  console.log("\n3. the real verbs still work, and the spellings people use");
  const help = host.invoke("help");
  check("help is a success result", help.kind === "success", help.kind);
  check("help documents every subcommand",
        ["status", "test", "help", "pdf"].every((v) => help.text.includes(v)), help.text.slice(0, 140));
  check("help explains that a sentence is allowed",
        help.text.includes("Anything else after /cad"), help.text.slice(0, 200));
  check("help is reachable with capitals too", host.invoke("HELP").kind === "success",
        host.invoke("HELP").kind);
  const status = host.invoke("status");
  check("status answers without running the pipeline",
        status.kind === "success" || status.text.includes("no jobs directory"),
        status.text.slice(0, 120));
  check("STATUS is accepted case-insensitively",
        !host.invoke("STATUS").text.includes("unknown subcommand"),
        host.invoke("STATUS").text.slice(0, 90));

  console.log("\n4. a path that does not exist is refused, and only when it looks like one");
  const nowhere = host.invoke("run Z:/definitely/not/here.pdf");
  check("a missing .pdf path is an error", nowhere.kind === "error", nowhere.kind);
  check("the error names the path",
        nowhere.text.includes("not/here.pdf") || nowhere.text.includes("not\\here.pdf"),
        nowhere.text.slice(0, 140));
  // A path with a space arrives wrapped in quotes, because that is how anyone pastes it.
  // The pair is removed; quote characters INSIDE the path are part of the name and stay.
  const quoted = host.invoke('"Z:/definitely/not/here either.pdf"');
  check("a fully quoted path has its quotes removed before it is classified",
        quoted.text.includes("here either.pdf") && !quoted.text.includes('"Z:'),
        quoted.text.slice(0, 140));
  const innerQuotes = host.invoke('Z:/definitely/not/"here too".pdf');
  check("quote characters inside a path are kept, not stripped",
        innerQuotes.text.includes('"here too"'),
        innerQuotes.text.slice(0, 140));

  if (!existsSync(FIXTURE)) {
    console.log(`\n  SKIP  the end-to-end run: fixture not found at ${FIXTURE}`);
  } else {
    console.log("\n5. an end-to-end run over a real drawing, from a bare path");
    const work = mkdtempSync(join(tmpdir(), "cadcmd-"));
    const previous = process.env.DSH_CAD_WORKSPACE;
    process.env.DSH_CAD_WORKSPACE = work;
    try {
      const started = Date.now();
      const result = host.invoke(`run "${FIXTURE}"`);
      console.log(`       ran in ${((Date.now() - started) / 1000).toFixed(1)}s -> ${result.kind}`);
      check("the run reports a result", result.kind === "success" || result.kind === "error", result.kind);
      check("the summary names the job directory", result.text.includes("job        :"), result.text.slice(0, 200));
      check("the summary reports the gate verdict", /self-check: (PASS|FAIL)/u.test(result.text));
      check("the summary reports geometric agreement", result.text.includes("geometric agreement :"));
      check("the summary reports the live dimension count", /dimensions : \d+/u.test(result.text));
      check("it names the DXF it wrote", result.text.includes("repro.dxf"));
      check("it names the difference image", result.text.includes("cmp_difference.png"));
      check("this sheet passes the gate", result.kind === "success",
            result.text.split("\n").filter((l) => l.includes("self-check")).join(" "));

      const job = join(work, "jobs", "steelwork-1to20");
      for (const artefact of ["extract.json", "dims.json", "repro.dxf", "build.json", "cmp.json",
                              join("trace", "trace_source.png"), join("trace", "trace_generated.png"),
                              join("out", "cmp_difference.png")]) {
        check(`wrote ${artefact}`, existsSync(join(job, artefact)), join(job, artefact));
      }
      const cmp = JSON.parse(readFileSync(join(job, "cmp.json"), "utf8"));
      check("the gate report says what the summary says", cmp.pass === (result.kind === "success"),
            `${cmp.pass} vs ${result.kind}`);

      console.log("\n6. status reprints the newest job without re-running it");
      const later = host.invoke("status");
      check("status found the job just run", later.text.includes("steelwork-1to20"), later.text.slice(0, 160));
      check("status repeats the gate verdict", /self-check: (PASS|FAIL)/u.test(later.text));
      check("status repeats the dimension count", /dimensions : \d+/u.test(later.text));
    } finally {
      if (previous === undefined) delete process.env.DSH_CAD_WORKSPACE;
      else process.env.DSH_CAD_WORKSPACE = previous;
      rmSync(work, { recursive: true, force: true });
    }

    console.log("\n7. a bare /cad falls back to the newest PDF in the workspace");
    const inbox = mkdtempSync(join(tmpdir(), "cadcmd-inbox-"));
    const previousWs = process.env.DSH_CAD_WORKSPACE;
    process.env.DSH_CAD_WORKSPACE = inbox;
    try {
      const before = host.invoke("");
      check("with no PDF anywhere it says so, and says how to fix it",
            before.kind === "error" && before.text.includes("attach a PDF"), before.text.slice(0, 140));
      writeFileSync(join(inbox, "dropped-here.pdf"), readFileSync(FIXTURE));
      const found = host.invoke("");
      check("after a PDF appears, a bare /cad uses it",
            found.text.includes("dropped-here") || found.text.includes("newest PDF in"),
            found.text.slice(0, 200));
      check("and it reports that it guessed",
            /source\s+:.*newest PDF/u.test(found.text),
            found.text.split("\n").slice(0, 4).join(" | "));
    } finally {
      if (previousWs === undefined) delete process.env.DSH_CAD_WORKSPACE;
      else process.env.DSH_CAD_WORKSPACE = previousWs;
      rmSync(inbox, { recursive: true, force: true });
    }
  }

  console.log("\n8. the bundle declares how it loads");
  const pkg = JSON.parse(readFileSync(join(PKG, "package.json"), "utf8"));
  check("package.json points at the module", pkg.main === "lib/index.js", pkg.main);
  check("it declares a dsh bundle patch", pkg.dsh?.bundle?.patch === "./cordis.patch.yml",
        JSON.stringify(pkg.dsh));
  check("the patch file exists", existsSync(join(PKG, "cordis.patch.yml")));
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
