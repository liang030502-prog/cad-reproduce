/**
 * `/cad` — drive the cad-reproduce pipeline from the Web GUI.
 *
 * Why a host command and not a prompt.  The pipeline is deterministic: given a PDF it
 * writes extract.json, dims.json, a DXF and a gate report, and the verdict is a number,
 * not an opinion.  Routing that through a model turn would cost tokens, take minutes of
 * agent reasoning to re-derive a fixed sequence, and make the result depend on how the
 * model felt about the instructions that day.  A command runs it directly against the
 * host, creates no model message, and costs no tokens.
 *
 * The skill's own scripts stay the single source of truth.  This file contains no CAD
 * logic: it locates `scripts/` inside the installed skill and runs the same entry points
 * a human would run by hand, in the same order, with the same flags.  If the pipeline
 * changes, this command changes with it and nothing here needs to know how.
 *
 * @module dsh-command-cad
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { basename, extname, isAbsolute, join, resolve } from "node:path";

const name = "command-cad";
const inject = ["commands"];

/** Where the skill is installed, in the order a reader would expect it to be found. */
function skillDir() {
  const candidates = [];
  if (process.env.DSH_CAD_SKILL) candidates.push(process.env.DSH_CAD_SKILL);
  candidates.push(join(homedir(), ".dsh", "skills", "cad-reproduce"));
  candidates.push(join(process.env.DSH_HOME ?? join(homedir(), ".dsh"),
                       "skills", "cad-reproduce"));
  for (const dir of candidates) {
    if (existsSync(join(dir, "scripts", "extract.py"))) return dir;
  }
  return undefined;
}

/** The interpreter the skill's scripts run under. */
function pythonExe() {
  return process.env.DSH_CAD_PYTHON ?? "python";
}

/** Root under which one directory per job is created. */
function workspace() {
  return process.env.DSH_CAD_WORKSPACE ?? process.cwd();
}

/** How long one stage may run before it is killed.  A large sheet takes minutes. */
function stageTimeoutMs() {
  const configured = Number(process.env.DSH_CAD_TIMEOUT_MS ?? "");
  return Number.isFinite(configured) && configured > 0 ? configured : 20 * 60 * 1000;
}

/** One script run, with its output kept for the failure path and its JSON read back. */
function runStage(script, args, { expect = [0] } = {}) {
  const started = Date.now();
  let stdout = "";
  let failed;
  try {
    stdout = execFileSync(pythonExe(), [script, ...args], {
      encoding: "utf8",
      timeout: stageTimeoutMs(),
      maxBuffer: 64 * 1024 * 1024,
      env: { ...process.env, PYTHONIOENCODING: "utf-8" },
      windowsHide: true,
    });
  } catch (error) {
    failed = error;
    stdout = `${error.stdout ?? ""}`;
  }
  const code = failed ? (typeof failed.status === "number" ? failed.status : 1) : 0;
  return {
    script: basename(script),
    code,
    ok: expect.includes(code),
    ms: Date.now() - started,
    stdout,
    stderr: failed ? `${failed.stderr ?? ""}` : "",
  };
}

function readJson(path) {
  try {
    return JSON.parse(readFileSync(path, "utf8"));
  } catch {
    return undefined;
  }
}

/** A short, honest rendering of the gate report: the numbers, then the verdict. */
function gateSummary(cmp) {
  if (!cmp) return ["self-check: (no report)"];
  const t = cmp.thresholds ?? {};
  const shares = (cmp.colour_shares ?? [])
    .slice()
    .sort((a, b) => b.delta - a.delta)
    .slice(0, 3)
    .map((r) => `${r.colour} ${r.source.toFixed(4)}/${r.generated.toFixed(4)} (d${r.delta.toFixed(4)})`);
  return [
    `self-check: ${cmp.pass ? "PASS" : "FAIL"}`,
    `  geometric agreement : ${cmp.geometric_agreement} (band ${t.band_mm} mm, need ${t.min_geometric_agreement})`,
    `  worst colour delta  : ${cmp.worst_colour_delta} (need <= ${t.colour_tolerance ?? t.tolerance ?? t.colour_share})`,
    `  worst colour IoU    : ${cmp.worst_colour_iou} (need >= ${t.min_colour_iou})`,
    `  colour shares       : ${shares.join("; ")}`,
  ];
}

/**
 * The one thing this command knows about how people actually type.
 *
 * A `/cad` line arrives either as a path, or as an attached PDF with a sentence beside it
 * - "complete this and export a PDF", "把这张图复刻成 DXF", or nothing at all.  Treating
 * the first word as a verb made every one of those sentences an "unknown subcommand",
 * which is a refusal to do the obvious thing.  So: the few words that ARE verbs are
 * verbs; everything else is a request to run.
 */
const VERBS = new Set(["status", "test", "help", "pdf", "run"]);

function stripQuotes(value) {
  return `${value ?? ""}`.trim().replace(/^["']|["']$/gu, "").trim();
}

function parseInput(rawInput) {
  const text = `${rawInput ?? ""}`.trim();
  if (text === "") return { verb: "run", rest: "" };
  const space = text.search(/\s/u);
  const head = (space === -1 ? text : text.slice(0, space)).toLowerCase();
  const rest = space === -1 ? "" : text.slice(space).trim();
  if (head === "run") return { verb: "run", rest: stripQuotes(rest) };
  if (head === "pdf") return { verb: "pdf", rest: stripQuotes(rest) };
  if (VERBS.has(head)) return { verb: head, rest };
  // Not a verb: a path, or a sentence that accompanies an attachment.
  return { verb: "run", rest: stripQuotes(text) };
}

/** Attachments arrive as durable references; a PDF among them is the source. */
function attachmentPdfs(invocation) {
  const out = [];
  for (const item of invocation.attachments ?? []) {
    if (item?.type !== "file") continue;
    for (const key of ["path", "localPath", "filename", "name", "url"]) {
      const value = item?.[key];
      if (typeof value !== "string") continue;
      const path = stripQuotes(value);
      if (extname(path).toLowerCase() === ".pdf") out.push(path);
      break;
    }
  }
  return out;
}

/** The newest PDF in a directory, so `/cad` alone can still do something useful. */
function newestPdfIn(dir) {
  try {
    const entries = readdirSync(dir)
      .filter((entry) => extname(entry).toLowerCase() === ".pdf")
      .map((entry) => join(dir, entry))
      .filter((path) => {
        try {
          return statSync(path).isFile();
        } catch {
          return false;
        }
      });
    if (entries.length === 0) return undefined;
    return entries.sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs)[0];
  } catch {
    return undefined;
  }
}

/**
 * Which PDF this invocation is about, and how it was found.
 *
 * An attached PDF wins over text, because the attachment is unambiguous and the text
 * beside it is usually a sentence.  Text wins only when it names a file that exists.
 */
function resolveSource(invocation, root) {
  const { rest } = parseInput(invocation.rawInput);
  const attached = attachmentPdfs(invocation);

  if (rest) {
    const asGiven = isAbsolute(rest) ? rest : resolve(root, rest);
    if (existsSync(asGiven)) return { path: asGiven, how: "path" };
    const looksLikePath = extname(rest).toLowerCase() === ".pdf";
    if (attached.length > 0) {
      return { path: attached[0], how: "attachment (the text beside it is not a file)" };
    }
    if (looksLikePath) return { error: `no such file: ${asGiven}` };
  } else if (attached.length > 0) {
    return { path: attached[0], how: "attachment" };
  }

  for (const dir of [root, join(root, "inbox")]) {
    const found = newestPdfIn(dir);
    if (found !== undefined) return { path: found, how: `newest PDF in ${dir}` };
  }
  return {
    error: "no PDF given, and no PDF to fall back on",
    hint: "attach a PDF to the message, or write the path: /cad C:\\path\\to\\drawing.pdf",
    rest,
  };
}

function usageText() {
  return [
    "Usage:",
    "  /cad <path-to.pdf>          reproduce it: extract -> dimensions -> DXF -> trace -> gate",
    "  /cad                        same, using the attached PDF or the newest PDF in the workspace",
    "  /cad pdf <path-to.pdf>      the above, then also try an AutoCAD PDF export",
    "  /cad status                 reprint the newest job's report, without re-running anything",
    "  /cad test                   run the skill's four self-check suites",
    "  /cad help                   this text",
    "",
    "Anything else after /cad is taken as the drawing to reproduce, so a sentence beside an",
    "attached PDF is fine: the attachment is used and the sentence is ignored.",
    "",
    "The pipeline runs directly in the host: no model message is created and no tokens are",
    "spent.  Reports, the DXF and the difference image land in a per-job directory.",
  ].join("\n");
}

/** An AutoCAD PDF export, attempted only when asked for. */
function pdfExportLines(skill, dxf, job) {
  const accad = join(skill, "scripts", "accad.py");
  if (!existsSync(accad)) return ["", `pdf export : skipped (${accad} is not installed)`];
  const r = runStage(accad, [dxf, "-o", join(job, "acad"), "--export",
                             "--json", join(job, "acad.json")],
                     { expect: [0, 1, 2] });
  const exported = join(job, "acad", `${basename(dxf, extname(dxf))}_export.pdf`);
  if (!existsSync(exported)) {
    const why = (r.stderr || r.stdout).split("\n").filter((l) => l.trim()).slice(-3).join(" ");
    return ["", `pdf export : no PDF produced — ${why.slice(0, 240) || "accad.py reported no error"}`];
  }
  return ["",
          `pdf export : ${exported}`,
          "             written by AutoCAD, so it is the authority on fonts, fills and lineweights."];
}

/** `/cad [run|pdf]` — create a job directory and run every stage in order. */
function commandRun(invocation, skill, { wantPdf = false } = {}) {
  const root = workspace();
  const found = resolveSource(invocation, root);
  if (found.error) {
    return { kind: "error", text: `${found.error}\n${found.hint ?? ""}\n\n${usageText()}` };
  }
  const source = found.path;
  if (extname(source).toLowerCase() !== ".pdf") {
    return { kind: "error", text: `not a PDF: ${source}` };
  }

  const stem = basename(source, extname(source)).replace(/[^\p{L}\p{N}._-]+/gu, "_");
  const job = join(root, "jobs", stem);
  mkdirSync(job, { recursive: true });

  const scripts = join(skill, "scripts");
  const config = join(skill, "cad-reproduce.yaml");
  const fail = (text) => ({ kind: "error", text });

  let r = runStage(join(scripts, "extract.py"), [source, "-o", join(job, "extract.json")]);
  if (!r.ok) return fail(`extract failed\n${r.stderr.slice(-1500) || r.stdout.slice(-1500)}`);

  // The dimension stage exits 2 when it cannot read the dimensions at all.  That is a
  // real answer, not a crash, so the run continues and says so rather than stopping.
  r = runStage(join(scripts, "infer_dims.py"),
               [join(job, "extract.json"), "-o", join(job, "dims.json"), "--config", config],
               { expect: [0, 2] });
  const dimsReadable = r.code === 0;
  if (!dimsReadable && r.code !== 2) {
    return fail(`dimension inference crashed\n${r.stderr.slice(-1500)}`);
  }

  r = runStage(join(scripts, "stage1_build.py"),
               [join(job, "extract.json"), join(job, "dims.json"),
                "-o", join(job, "repro.dxf"), "--json", join(job, "build.json")]);
  if (!r.ok) return fail(`build failed\n${r.stderr.slice(-1500)}`);

  r = runStage(join(scripts, "trace.py"),
               [join(job, "extract.json"), "--dims", join(job, "dims.json"),
                "--out-dir", join(job, "trace"), "--dpi", "300"]);
  if (!r.ok) return fail(`trace failed\n${r.stderr.slice(-1500)}`);

  // compare.py's exit code IS the verdict: 0 pass, 1 fail.  Neither is a crash.
  r = runStage(join(scripts, "compare.py"),
               [join(job, "trace", "trace_source.png"),
                join(job, "trace", "trace_generated.png"),
                "-o", join(job, "out"), "--dpi", "300",
                "--json", join(job, "cmp.json"),
                "--extract", join(job, "extract.json")],
               { expect: [0, 1] });

  const extract = readJson(join(job, "extract.json"));
  const dims = readJson(join(job, "dims.json"));
  const build = readJson(join(job, "build.json"));
  const cmp = readJson(join(job, "cmp.json"));
  const usable = (dims?.proposals ?? []).filter(
    (p) => p.value !== undefined && p.value !== null && ["high", "medium"].includes(p.confidence));

  const dxf = join(job, "repro.dxf");
  const lines = [
    `job        : ${job}`,
    `source     : ${source}   (${found.how})`,
    `paths      : ${extract?.stats?.path_count ?? "?"}   dashed: ${extract?.stats?.dashed_paths ?? "?"}`,
    `dimensions : ${build?.dimensions_emitted ?? "?"} live DIMENSION entities` +
      (dimsReadable ? `   (${usable.length} placed at high/medium confidence)` : "   (STAGE FAILED — none inferred)"),
    `entities   : ${JSON.stringify(build?.entity_counts ?? {})}`,
    ...gateSummary(cmp),
    "",
    `dxf        : ${dxf}`,
    `difference : ${join(job, "out", "cmp_difference.png")}  (grey=agree, blue=extra, red=missing)`,
  ];
  if (!dimsReadable) {
    lines.splice(5, 0,
      "NOTE       : infer_dims exited 2 — it could not read this sheet's dimensions.",
      `             Its evidence table: ${pythonExe()} "${join(scripts, "infer_dims.py")}" ` +
      `"${join(job, "extract.json")}" -o "${join(job, "dims.json")}" --config "${config}"`);
  }
  if (wantPdf) lines.push(...pdfExportLines(skill, dxf, job));

  return { kind: cmp?.pass ? "success" : "error", text: lines.join("\n") };
}

/** `/cad status` — re-print the newest job's report without re-running anything. */
function commandStatus(root) {
  const jobsRoot = join(root, "jobs");
  if (!existsSync(jobsRoot)) return { kind: "error", text: `no jobs directory at ${jobsRoot}` };
  const jobs = readdirSync(jobsRoot)
    .map((entry) => join(jobsRoot, entry))
    .filter((p) => {
      try {
        return statSync(p).isDirectory() && existsSync(join(p, "cmp.json"));
      } catch {
        return false;
      }
    })
    .sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs);
  if (jobs.length === 0) {
    return { kind: "error", text: `no completed job under ${jobsRoot}; run /cad <path-to.pdf> first` };
  }
  const job = jobs[0];
  const cmp = readJson(join(job, "cmp.json"));
  const build = readJson(join(job, "build.json"));
  const dims = readJson(join(job, "dims.json"));
  const usable = (dims?.proposals ?? []).filter(
    (p) => p.value !== undefined && p.value !== null && ["high", "medium"].includes(p.confidence));
  return {
    kind: cmp?.pass ? "success" : "error",
    text: [
      `job        : ${job}   (${jobs.length} completed job(s); this is the newest)`,
      `dimensions : ${build?.dimensions_emitted ?? "?"} live DIMENSION entities` +
        (usable.length ? `   (${usable.length} placed at high/medium confidence)` : ""),
      `entities   : ${JSON.stringify(build?.entity_counts ?? {})}`,
      ...gateSummary(cmp),
      "",
      `difference : ${join(job, "out", "cmp_difference.png")}`,
    ].join("\n"),
  };
}

/** `/cad test` — the skill's four suites, which is what "is the skill healthy" means. */
function commandTest(skill) {
  const suites = [
    "test_repository.py",
    "test_regressions.py",
    "test_arc_recognition.py",
    "test_steelwork_sheet.py",
  ];
  const lines = [`skill      : ${skill}`, ""];
  let allOk = true;
  for (const suite of suites) {
    const script = join(skill, "evals", suite);
    if (!existsSync(script)) {
      lines.push(`  SKIP  ${suite}  (not installed)`);
      continue;
    }
    const r = runStage(script, []);
    const failed = r.stdout.split("\n").filter((l) => l.includes("[FAIL]"));
    if (!r.ok) allOk = false;
    lines.push(`  ${r.ok ? "PASS" : "FAIL"}  ${suite}  (${(r.ms / 1000).toFixed(1)}s)`);
    for (const line of failed.slice(0, 5)) lines.push(`          ${line.trim()}`);
    if (!r.ok && failed.length === 0) lines.push(`          ${(r.stderr || r.stdout).slice(-400)}`);
  }
  return { kind: allOk ? "success" : "error", text: lines.join("\n") };
}

/**
 * Register `/cad` against the receiving agent.
 *
 * `input.attachments` is what lets a PDF be dropped into the composer and sent with the
 * command: the host admits the attachment and hands the handler a durable reference, so
 * the command never re-reads the uploaded bytes.
 */
function apply(ctx) {
  ctx.commands.register({
    name: "cad",
    description: "reproduce a PDF engineering drawing as an editable DXF/DWG and self-check it",
    input: { hint: "[<pdf>|pdf|status|test|help]", attachments: true },
    handler: (invocation) => {
      const skill = skillDir();
      if (skill === undefined) {
        return {
          kind: "error",
          text: "the cad-reproduce skill was not found. Install it at " +
                "~/.dsh/skills/cad-reproduce, or set DSH_CAD_SKILL to its directory.",
        };
      }
      const { verb } = parseInput(invocation.rawInput);
      switch (verb) {
        case "status":
          return commandStatus(workspace());
        case "test":
          return commandTest(skill);
        case "help":
          return { kind: "success", text: usageText() };
        case "pdf":
          return commandRun(invocation, skill, { wantPdf: true });
        default:
          return commandRun(invocation, skill);
      }
    },
  });
}

export { apply, inject, name };
