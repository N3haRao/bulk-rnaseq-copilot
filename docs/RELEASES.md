# Code bank releases on the server

How a change to the lab's code bank reaches the website, step by step. Everything happens on the server (Ada, or the GPU server later) with local git. GitHub is not needed.

**In short:** a bioinformatician releases the code bank. The site checks itself against the release in a staging copy, and the release agent (an open-weight coding model) fixes the site there if needed. The same bioinformatician reads the review and approves it, and the site restarts with the new version. Nothing goes live without that approval, and every step can be undone.

## The folders

All in `$COPILOT_HOME` (for example `~/rnaseq-copilot` or `/project/<lab>/rnaseq-copilot`). `containers/ada/setup_home.sh` makes them once.

```
rnaseq-copilot.sif  ollama.sif       the images: software only (R, tools, Python, Node, Ollama)
copilot/                             the LIVE site: a git repository. Approved releases are merged into it.
codebank/                            the code bank: bioinformaticians commit here and tag releases
codebank-releases/v0.6.0/ ...        one read-only checkout per release (git worktrees of codebank/)
codebank-live -> codebank-releases/v0.6.0    the release the site uses
staging/v0.6.1/site/                 the staging copy of the site for a pending release (branch release/v0.6.1)
staging/v0.6.1/RELEASE_REVIEW.md     what changed, what the agent did, the checks, the full diff
staging/v0.6.1/agent_log.jsonl       every action of the release agent and its result
release-state.json                   live release, pending release, history (who released, who approved, when)
models/  data/  secrets.env          models; uploads and runs (private); optional API key
```

The site job (`containers/ada/site.slurm`) runs the image with `$COPILOT_HOME` mounted at the same path. The site code then comes from `copilot/` and the code bank from `codebank-live`, not from the image. A release therefore needs only a restart of the site. Rebuild the image only when the software changes: `containers/env-*.yml`, `backend/requirements.txt` or `frontend/package-lock.json`. The site's log warns about the last two.

## 1. Release the code bank (bioinformatician)

In `$COPILOT_HOME/codebank`, after committing the change:

1. Raise the version in three places: `CODE_BANK_VERSION` in `R/utils.R`, `version:` in `manifest/tools.yaml`, and a `## v0.6.1` entry in `CHANGELOG.md` that says what changed.
2. Run the release script:

   ```bash
   scripts/release.sh 0.6.1            # add --quick to skip the long main-pipeline tests
   ```

`release.sh` stops before tagging if:

- something is uncommitted;
- the three version numbers disagree;
- `tests/check_manifest.R` fails, meaning the manifest does not match the scripts' defaults or uses a site building block that does not exist;
- the tests of a module changed since the last release fail.

If those pass, it tags `v0.6.1` with `released-by: <your user name>` and runs the website check, `containers/ada/release.slurm`. Where there is SLURM it submits it as a job. Without SLURM (fuji), or with `RELEASE_WITH_SLURM=0`, it runs it right away in the terminal. Either way the job starts the coding model, or reuses the running site's model server.

A new optional analysis needs only the code bank. Describe its inputs, form and results in the module's `site:` block in the manifest (see `codebank/docs/ADDING_A_MODULE.md`), and the site shows it with no website code. The release agent is for what the building blocks cannot express yet, such as a new kind of upload.

## 2. The website check (automatic, `ops/release integrate`)

The release job:

1. Checks out the release into `codebank-releases/v0.6.1`.
2. Makes a staging copy of the live site on the git branch `release/v0.6.1`.
3. Runs two checks against the new code bank:
   - `manifest`: the site can show every module's `site:` block;
   - `tests_quick`: the contract test plus the unit tests.
4. Only if a check fails, starts the release agent: `qwen3-coder:30b` through Ollama, set with `RELEASE_AGENT_MODEL`. Its limits are below.
5. Runs the final checks itself, whatever the agent says: `manifest` and the full test suite (`RELEASE_FINAL_TESTS`, default `tests_full`). If the agent changed the frontend, a type check and build are added.
6. Commits the staging copy and writes `RELEASE_REVIEW.md`.

The release agent:

- reads the new code bank (manifest, `CHANGELOG.md`, `docs/ADDING_A_MODULE.md`);
- edits only the staging copy, and only in `backend/app`, `backend/tests`, `frontend/src`, `docs`, `README.md` and `containers/env-*.yml`;
- can never edit:
  - the code bank: methods belong to the bioinformaticians;
  - user data or git internals;
  - the release tooling or `guardrails.py`;
  - the contract test it has to pass;
- can never run commands of its own: only four fixed checks (`manifest`, `tests_quick`, `tests_full`, `frontend`);
- logs every action in `agent_log.jsonl`.

The job log ends with **READY** or **NEEDS ATTENTION** and the path of the review. A release the site already handles takes minutes and needs no model.

**Speed.** On a GPU, for example fuji's A30 (24 GB, the model fully on the GPU), a release that needs changes takes about 10–30 minutes. On Ada's CPUs it can take hours. `qwen3-coder:30b` is a mixture-of-experts model with about 3B active parameters per token, so it runs on CPUs, just slowly.

## 3. Review (the bioinformatician who released it)

```bash
less $COPILOT_HOME/staging/v0.6.1/RELEASE_REVIEW.md
$COPILOT_HOME/copilot/ops/release preview v0.6.1      # optional: the staging site on port 8001 (tunnel to it like the site)
```

The review shows:

- the CHANGELOG entry;
- the checks before and after the changes;
- the agent's summary and its "look at by hand" list;
- a warning if it touched test files;
- the full diff of the site.

Check the summary against the diff. To reject the release, do nothing: the live site is unchanged. Then fix the code bank and release a new version.

## 4. Approve

```bash
$COPILOT_HOME/copilot/ops/release approve v0.6.1
```

Approval is refused if:

- the release is not ready (a check failed);
- you are not the person who made the release. An admin can use `--override "reason"`, and the reason is recorded;
- the staging copy changed after the review was written;
- the live site folder has uncommitted changes.

On approval:

1. The staging branch is merged into `copilot/`.
2. `codebank-live` is pointed at `v0.6.1`.
3. Who approved and when are recorded in `release-state.json`.
4. The site restarts with `$COPILOT_RESTART_CMD`. `setup_home.sh` suggests `containers/ada/restart_site.sh`, which restarts the SLURM job, or the background process where there is no SLURM. The new log has the new tunnel port.

Runs started before the restart keep the code bank they started with. Every run records the exact code bank commit.

## Undo, status

```bash
$COPILOT_HOME/copilot/ops/release status          # live and pending release
$COPILOT_HOME/copilot/ops/release rollback --yes  # back to the previous release (site code and code bank), then restart
```

## Where the commands run

`ops/release` uses the image's Python, R and Node when `$COPILOT_HOME/rnaseq-copilot.sif` exists, so nothing needs installing on the server. On a laptop with `backend/.venv`, it uses that instead. Settings:

| Variable | Default | What |
|---|---|---|
| `COPILOT_HOME` | the folder above `copilot/` | the layout above |
| `RELEASE_AGENT_MODEL` | `qwen3-coder:30b` | the coding model (pull with `containers/ada/pull_model.sh`) |
| `RELEASE_AGENT_CONTEXT` | `32768` | the model's context window, in tokens |
| `RELEASE_AGENT_TIMEOUT` | `1200` (`3600` on CPUs) | seconds per model answer |
| `RELEASE_FINAL_TESTS` | `tests_full` | final test set (`tests_quick` is faster) |
| `RELEASE_WITH_SLURM` | `1` | `0`: `release.sh` runs the check directly instead of as a job |
| `COPILOT_RESTART_CMD` | none | how to restart the site after approve / rollback |

## Tests

`backend/tests/test_release.py` runs the whole flow with a scripted stand-in for the model:

- a release that needs no change;
- a release that needs a new upload handler (forbidden edits are refused, then approve and rollback);
- a model that stops answering (not approvable);
- the workspace limits.
