# Verification

## Caligo named attempts — October 16, 2026

The CLI/package is `caligo`, with `.caligo/` project state and `CALIGO_*` environment variables. Old command forms, the old package entrypoint, automatic dev-to-coding synchronization, and shared writable Git metadata have been removed. No state migration or compatibility aliases are implemented.

`caligo create NAME --mode user|supervisor` creates and enters a named attempt. `caligo list` lists attempts in the current project and marks the current one. `caligo switch NAME` leaves the current coding shell and opens the target attempt under its access policy. Each attempt has an independent Git repository outside the declared project files.

Verified with the installed CLI and Sandbox Runtime 0.0.79 on macOS:

- A real pseudo-terminal entered `baseline`, edited a file, created `dropout` from inside the shell, listed both attempts, switched back, and verified the edit and Git status.
- Inside the sandbox, only `src`, `data`, and `executable` appeared in the test workspace. Reads of another attempt, its Git configuration, the original manifest, and the fixed evaluator failed. Writes to read-only data and undeclared workspace root entries failed.
- `caligo exec` streamed stdout and stderr and returned exit code 7 from a separate dev copy. Remote edits did not overwrite coding files, including with coding and dev both configured as local.
- Duplicate names and unknown attempts fail without replacing work. Names are scoped to the project directory. Each attempt has independent Git metadata and sanitized initial history.
- Separate filesystem-backed hosts exercised coding/dev/experiment routing, excluded fixed setup from dev, and preserved frozen submitted inputs and project/attempt provenance. Slurm submission was stubbed for these routing checks.
- Existing tests verify failed experiment evidence and real S3-protocol input/output transfers using a disposable local endpoint.
- A separate shell check ran installed `codex --version` successfully. Codex login and an actual model turn were not performed.

`.venv/bin/python -m pytest -q`: **10 passed, 1 skipped**, in 10.72 seconds. The experiment Docker test is opt-in and was skipped because the daemon was stopped. No VM was started and no cluster allocation was requested. The local Sandbox Runtime installation occupies 16 MB. CPU/RAM overhead was not benchmarked. Linux coding isolation and live SSH/Slurm routing for this revision have not been exercised.

## Historical verification (superseded interfaces)

The material below records earlier implementations. Its command names, workspace behavior, and paths are not the current interface and are not supported through aliases or migrations.

## Explicit coding, dev, and experiment locations

The new CLI requires `locations.coding`, `locations.dev`, and `locations.experiments` with no inferred locations. Coding/dev use native worktrees and host tools. Full experiments retain the Slurm/container backend.

Before the creation-only change, `RESEARCH_TEST_DOCKER=1 .venv/bin/python -m pytest -q`: **10 passed**. After that change, the existing non-container checks passed. The container check could not run because the Docker daemon was stopped.

- Before the creation-only change, a real interactive local terminal was opened with `research create`; `python3 train.py` produced `[2, 4, 6]`, `git status` worked, and `research list` showed the active worktree. The configured experiment host was deliberately unused. No Docker, SSH connection, or scheduler allocation was needed for this session. Shell entry now requires explicit `research shell`.
- Tests exercise independent coding/dev/experiment routes using separate filesystem-backed hosts, actual native subprocesses, and the session CLI. They verify that ordinary commands execute at dev and only `research run` submits to the experiment scheduler; submitted dirty files survive subsequent edits.
- Location tests block SSH/container/scheduler executables for the local-only path, and verify missing location choices fail validation.
- Cross-host file synchronization rejects concurrent source edits and retains the dev copy.
- A disposable Linux SSH host verified native dev execution, the reverse session socket, and file synchronization back to local coding. From that SSH dev shell, `research run` correctly targeted the original local machine. Slurm submission was stubbed; the generated batch script was then executed directly, running a real Docker experiment and fixed evaluation successfully. The temporary SSH server was removed after verification. Live Slurm submission through the new routing has not been exercised.
- The obsolete format-level tests and interactive-container-only checks were removed. Experiment container and bucket-storage checks remain.

The older verification below predates this location split. Its live Slurm results establish the original execution backend behavior, not end-to-end verification of the new routing.

## Historical remote-only verification

Verified on October 15, 2026, using the actual ORCD account `hongning` through the existing `orcd` SSH alias. This implementation uses Slurm and Apptainer; no SkyPilot deployment or GPU allocation was needed for the smoke experiment.

## Behavior checks

`RESEARCH_TEST_DOCKER=1 .venv/bin/pytest -q`: **5 passed**.

The small suite checks five user-visible behaviors rather than private command layouts:

1. Separate real Git worktrees keep edits isolated and exclude fixed setup and source repository history.
2. Submitted runs retain dirty/ignored files, respect script selection, and remain connected to the submitting worktree as editing continues. Worktree filtering excludes other environments' runs.
3. Failed runs retain their inputs and exit outcome even when a later scheduler observation fails.
4. Real Docker containers enforce the two permission modes, persist explicit Git operations, hide fixed setup interactively, and execute a frozen submission plus fixed evaluation.
5. Real S3-protocol transfers copy inputs without writing changes back and publish failed-run evidence to a separate output location.

`research init` followed by `research validate` also succeeded on a fresh temporary project. The checked-in example is manually prepared and validates through the same interface.

## Live ORCD execution

Project: `project-20261015-191336-853687a98b`.

| Environment | Mode | Branch |
|---|---|---|
| `env-20261015-191806-6df6e5823f` | supervisor | `env-20261015-191806-6df6e5823f` |
| `env-20261015-191931-e28bfda009` | user | `env-20261015-191931-e28bfda009` |

The worktrees reside beneath `/home/hongning/.local/share/research/projects/project-20261015-191336-853687a98b/worktrees/`. They use a shared Git repository containing only exposed inputs. Image `python:3.12-bookworm` was resolved to a cached SIF and its digest recorded.

A `research exec` invocation in the user environment verified the actual Apptainer sandbox: the initial directory was `executable/`; writes within `src/` persisted; writes to `data/` and new root directories failed; `/fixed` and the controller's fixed-setup path were absent; and fixed evaluation files did not appear in accessible Git history.

`research run` submitted `run-20261015-192500-60e3c23112` as Slurm job **25966123**, using `cpu:1`, partition `mit_quicktest`, and the declared image. `research status` confirmed `COMPLETED`, Slurm exit `0:0`, and zero exit codes for both experiment and fixed evaluation.

`research artifacts` downloaded that run to `examples/minimal/.research/downloads/run-20261015-192500-60e3c23112`. Its log contains predictions `[2, 4, 6]` and a passed fixed evaluation; `artifacts/metrics.json` contains `{"passed": true}`. Submitted file contents match their recorded SHA-256 hashes. Archive extraction removes group/other write permissions on downloaded files; the original submitted modes remain recorded in `run.json`.

An explicit override, `executable/failure.py`, submitted `run-20261015-192645-64ebf43947` as job **25966594**. It intentionally exited with code 7. Slurm accounting confirmed `FAILED`, exit `7:0`; `research logs` returned `intentional failure`; and artifact download retained `partial.txt` containing `preserved after failure`.

`research list --env env-20261015-191931-e28bfda009` returned only that user worktree and its failed run, excluding supervisor runs. The record exposed the submitting path, branch, commit `37efc71dd044dc4d71fc3c5fd5abf52343a1bf28`, uncommitted script/edit summary, and snapshot hash. This also verified the association for runs created before the explicit worktree-path field was added.

An initial run, job 25965259, exposed the difference between login-node and compute-node runtime availability. ORCD needs `scheduler.setup = "source /etc/profile && module load apptainer"` in its local project configuration. Runtime module setup is now explicit configuration rather than an inferred site default. The failed run's evidence was retained. Interactive checking also exposed an NFS shared-lock descriptor issue; the lock file is now opened for both reading and writing.

Once a finished job aged out of `squeue`, live checking exposed its invalid-job-ID response. Status lookup now continues to `sacct` in that case; the recorded `7:0` result above was verified after this correction.

## Entering a sandbox as a human

`research create examples/minimal/project.toml --mode user` opened an interactive Apptainer shell directly, without a separate `shell` command. This created `env-20261015-194009-bc7ed53b42` in the project above.

Commands typed into that shell verified Linux execution, the `executable/` working directory, a persistent writable `src/inside-shell.txt`, denied writes to `data/` and the workspace root, and absence of fixed setup and the laptop's filesystem. `research list` showed only this worktree.

Still inside the same shell, `research run` submitted `run-20261015-194116-59f68f3686` (Slurm **25969365**). `research status` returned `SUCCEEDED` and scheduler `COMPLETED`, exit `0:0`; logs showed predictions and passed evaluation; `research artifacts` extracted the run and `metrics.json` contained `{"passed": true}`. The run retained this worktree's branch, commit, path, and uncommitted edit. Attempting to inspect another worktree's run through the session client was rejected. `exit` ended the container shell and returned to the local terminal. No agent was used for this check.

The small test suite also exercises submission through the session's actual CLI/socket interface, without asserting its internal command layout.

## Portability

A fresh `research init` project validated with no configured host, partition, account, or module setup. The shipped Python code, tests, and public example contain no ORCD hostname, MIT partition, personal username, or personal home path. The SSH user's home is resolved at deployment time. Runtime, scheduler options, image, remote root, and storage destinations are project settings.

A second fresh project, `project-20261015-194515-7939f3cd15`, verified the portable configuration path using only local settings for `remote.host`, `scheduler.partition`, and `scheduler.setup`. `research create` entered `env-20261015-194913-b2a9a0538f`; shell permission checks passed. From that shell, run `run-20261015-195021-f25cf3f5e2` (Slurm **25971097**) completed successfully, including the fixed evaluation, with scheduler exit `0:0`. Its recorded dirty contents included `src/entered.txt` and its branch matched the current worktree.

ORCD-specific verification configuration is kept under the ignored `examples/minimal/.research/orcd/` directory. The supported deployment target is currently a Linux Slurm host using Apptainer/Singularity or Docker, with a macOS/Linux client. Other scheduler backends and Windows clients have not been implemented.

## Scope

These checks exercised CPU jobs on ORCD, actual container permissions, and a disposable S3-compatible endpoint. They did not run a GPU training workload or use a production cloud bucket. GPU requests are passed to Slurm and GPU devices are exposed only during scheduled execution. Interactive commands never submit a scheduler job. The runtime and scheduler implementations remain small, separate modules.

The executable-directory rule follows the user's clarification: enforce the launch working directory, not where interpreters may read source files. The implementation is for trusted users with host access, as specified in `Agent.md`.
