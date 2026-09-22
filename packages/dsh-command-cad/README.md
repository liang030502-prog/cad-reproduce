# dsh-command-cad

A host slash command that drives this repository's pipeline from the DSH Web GUI.

```
/cad <path-to.pdf>     reproduce it: extract -> dimensions -> DXF -> trace -> gate
/cad                    same, using the attached PDF or the newest PDF in the workspace
/cad pdf <pdf>          the above, then also try an AutoCAD PDF export
/cad status             reprint the newest job's report, without re-running anything
/cad test               run the skill's four self-check suites
/cad help               usage
```

A PDF can also be dropped into the composer and sent with the command, since it declares
`input.attachments`.

## A sentence is not a subcommand

The first version treated the first word after `/cad` as a verb, so a user who attached a
drawing and typed what they wanted — `完成并输出为pdf` — got
`unknown subcommand 完成并输出为pdf`.  That is a refusal to do the obvious thing.

Only these are verbs: `status`, `test`, `help`, `pdf`, `run` (case-insensitive).
**Anything else is the drawing to reproduce.**  So all of these work:

| what you type | what happens |
|---|---|
| `/cad` with a PDF attached | runs on the attachment |
| `/cad 完成并输出为pdf` with a PDF attached | runs on the attachment; the sentence is ignored |
| `/cad C:\drawings\beam.pdf` | runs on that file |
| `/cad run "C:\my drawings\beam.pdf"` | same, quotes and all |
| `/cad` with no text and no attachment | uses the newest `.pdf` in the workspace, and says it guessed |
| `/cad 完成并输出为pdf` with nothing attached and no PDF around | asks for a PDF, and explains both ways to give one |

Text is only treated as a path when it actually ends in `.pdf`; a sentence that ends in
something else is not reported as a missing file.  Quotes *around* a path are removed,
because that is how a path containing a space gets pasted; quote characters *inside* a
path are part of the name and stay.

## Why a command and not a prompt

The pipeline is deterministic.  Given a PDF it writes `extract.json`, `dims.json`, a DXF
and a gate report, and the verdict is a number rather than an opinion.  Routing that
through a model turn would cost tokens, spend minutes re-deriving a fixed sequence, and
make the outcome depend on how the model read the instructions.  A command runs it
directly against the host, creates no model message, and costs no tokens.

This package contains **no CAD logic**.  It locates `scripts/` inside the installed skill
and runs the same entry points, in the same order, with the same flags a person would run
by hand.  Change the pipeline and this command follows; nothing here needs to know how it
works.

## Install

```bash
# 1. put the plugin where the profile can resolve it
dsh plugin add --profile web /absolute/path/to/packages/dsh-command-cad

# 2. add the bundle and let the profile compose it
#    (the row is inserted by this bundle's own cordis.patch.yml)
```

Then add `"dsh-command-cad"` to `dsh.profile.bundles` in the profile's `package.json`
(`$DSH_HOME/profiles/<profile>/package.json`), or insert the row by hand in that profile's
`cordis.patch.yml`:

```yaml
- insert:
    - id: command-cad
      name: 'dsh-command-cad'
```

A profile with `patchReload: live` picks the new row up without restarting the server;
otherwise restart the `dsh web` process.  Type `/` in the composer to confirm the command
appears in the discovery menu.

## Configuration

Read from the environment of the `dsh` process:

| variable | default | meaning |
|---|---|---|
| `DSH_CAD_SKILL` | `~/.dsh/skills/cad-reproduce` | where the skill is installed |
| `DSH_CAD_WORKSPACE` | the process working directory | where `jobs/<name>/` is created |
| `DSH_CAD_PYTHON` | `python` | interpreter for the skill's scripts |
| `DSH_CAD_TIMEOUT_MS` | 20 minutes | per-stage timeout |

## Test

```bash
node test/run.mjs
```

Behavioral: it registers the command against a stub host, invokes the handler the way the
adapter does, runs a real end-to-end job over the drawing in
`evals/fixtures/steelwork-1to20.pdf`, and asserts on the result text and the files on
disk.  A test that only checked `register` was called would pass on a command that cannot
run anything.
