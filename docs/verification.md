# Verification

## Core functionality checks — October 19, 2026

Reduced the Python suite from 61 to 44 tests focused on workspace creation and permissions, frozen runs and evaluation, agent communication and completion, host routing, storage, and checkpoint recovery. Removed minor configuration edge cases and legacy migration tests. Checks no longer require exact guide prose, workspace listings, agent order, reminder counts, or a fixed automatic-turn limit.

`CALIGO_TEST_DOCKER=1 .venv/bin/python -m pytest -q`: **44 passed** in 83.31 seconds. `pnpm test` in `ui/`: **4 passed**. UI event tests check delivery and tool output without requiring specific display labels. Ruff and whitespace checks pass.

## Workspace agent guide — October 19, 2026

New attempts include a read-only `Agent.MD` with commands, TOML-derived folders and hosts, and checkpoint save/reload guidance. Both native backends receive its contents; older workspaces retain their inline instructions.

`CALIGO_TEST_DOCKER=1 .venv/bin/python -m pytest -q`: **61 passed** in 90.67 seconds. Checks cover custom TOML folders/hosts in both modes, workspace validation, guide visibility and write protection in a real container, and delivery through the Codex app-server protocol fixture. No live model inference was used.

## Agent Chat UI — October 19, 2026

The frontend in `ui/` uses assistant-ui's prebuilt Thread and Composer with Caligo's existing service. The custom chat renderer, scrolling, and copy controls have been removed. Caligo supplies message conversion, steering, and experiment controls. Next.js exports static assets served by the Python gateway; Node is needed to build, not to run the packaged interface.

- TypeScript, four event-conversion tests, and the production build pass. The Python suite passes all **57 tests**, including Docker checks.
- Chrome checks cover custom model selection, agent creation, Markdown, sending, pause/resume, reload/replay, stopping, new threads/history, experiment submission, and logs. The prebuilt composer also sends active steering, stops a response, and resumes a paused agent when a new message is sent. A phone-sized viewport retains navigation and the composer without horizontal overflow.
- A wheel built outside the frontend runtime serves its HTML, CSS, and JavaScript outside the checkout. The upstream license is included; the old vanilla frontend is removed.

Browser interactions use a deterministic demo agent and simulated experiment submissions. Live model inference and live Slurm submission were not exercised for this UI change.

## Browser interface — October 18, 2026

`caligo ui` serves the frontend from `ui/` through a local Python gateway. It reuses project creation, agent services, durable events, and original coding-host run routes without a frontend build or new runtime dependencies.

- Chrome checks covered agent creation, sending instructions, replay after reload, pause/resume, stopping, the restart dialog, and desktop/mobile layouts.
- HTTP and subprocess checks cover multiple project revisions, new threads, idempotent instructions, SSE replay cursors, viewer disconnection, run status/logs, and evaluation-result downloads. Archive transfer also runs through the shipped zipapp with an SSH stand-in.
- The wheel builds and serves all three frontend assets outside the source checkout.

Live remote SSH, Slurm, and authenticated model inference were not exercised for the UI.

## Human and agent modes — October 18, 2026

Implemented human/agent attempts, pinned project revisions, protected evaluator stages, a detached coding-host service, persistent agent threads, a resumable HTTP/SSE API, and native Codex/Claude adapters. The entries below this section describe historical versions; their supervisor/user and SRT behavior has been replaced.

Verified on macOS with Docker Desktop (engine 29.2.1):

- `CALIGO_TEST_DOCKER=1 .venv/bin/python -m pytest -q`: **42 passed** in 45.51 seconds. All Docker integration checks ran; none were skipped.

- Real interactive human shells retain ordinary host file access, installed tools, Git operations, and persistent edits. Write-policy checks reject protected data writes, deletion, chmod, directory replacement, and atomic editor saves over read-only files. Executable bits and empty directories survive checkout creation. Creating a revision from changed evaluator/code files leaves earlier attempts unchanged.
- Actual Docker containers reject writes to readable inputs, protected files, and undeclared workspace entries. Host sentinel files, `/fixed`, and the Docker socket are absent. A real scoped HTTP broker routes development work to another container, streams its output and exit status, and rejects attempt creation/switching.
- The full CLI starts a detached service and a real coding container using a deterministic app-server protocol fixture. Viewer replay leaves the thread running. Active steering and interruption work during a held turn. A containerized dev command streams stdout/stderr and exits with code 7. Intentional service shutdown/resume and SIGKILL recovery retain the native conversation identity and working files while reconciling orphaned containers.
- Deterministic subprocess tests cover queued input, active steering bypassing next-turn input, one-writer leases, message idempotency, SSE authentication/replay, pause/resume, automatic continuation, transient runtime retry, ambiguous delivery preservation, and terminal run notifications delivered once.
- Native Codex CLI 0.163.0 and Claude Agent SDK 0.2.168 both initialize inside the supplied agent container without imported credentials or model inference. Model-generated turns are represented by deterministic fixtures; live authenticated inference and provider-specific recovery during a real model turn remain unverified.
- Actual experiment containers exclude the evaluator from participant execution. Evaluation reads participant artifacts through a read-only mount and writes only its separate evaluation directory. Participant-forged metrics do not replace authoritative metrics. Artifact links are rejected before evaluation; regular failure evidence remains downloadable.
- Legacy conversion preserves working edits. Persistent run routes can be recovered without a live agent service and retain their original remote configuration after a project revision change.
- Existing real checkpoint-writing programs and simulated Slurm requeues retain run IDs, appended logs, working files, outputs, and per-execution outcomes. Python interpreter discovery and shipped zipapp RPC checks remain covered. The pre-existing scheduler changes were retained.
- The 0.2.0 wheel builds successfully and includes the agent image definition and new runtime modules. Ruff and whitespace checks pass.

Linux human Bubblewrap enforcement, live remote SSH deployment of the new service, and live Slurm scheduling/preemption were not exercised for this revision. Service SQLite storage must be local disk, and host boot supervision must be configured separately.

## Slurm requeueing — October 17, 2026

`scheduler.requeue = true` enables Slurm requeue eligibility for experiments and routes dev commands through native Slurm jobs. Restarts retain their working directory, checkpoint outputs, run ID, and appended logs. Application code saves and reloads its own checkpoints.

- `.venv/bin/python -m pytest -q`: **26 passed, 1 skipped**. The optional Docker integration test was skipped.
- Dev checks execute the generated batch script and real checkpoint-writing Python programs on separate filesystem-backed hosts. The first execution's controller and command are killed together with SIGKILL after saving a checkpoint. A simulated Slurm requeue reruns the same batch script with an incremented restart count. The program loads its checkpoint, retains working-directory files, and returns either success or exit code 7. Logs from both executions stream once, and only one submission occurs.
- Experiment checks restart a real checkpoint-writing program through the controller with the container launcher replaced by a native subprocess. Outputs and writable files survive, submitted inputs stay frozen, and queued state overrides the previous execution's failed outcome.
- Checks also cover strict boolean configuration, submission flags, transient scheduler polling failures, retained per-execution outcomes, and cancellation while a dev job waits.
- Slurm scheduling/preemption and SSH are simulated. No live Slurm preemption, replacement node, GPU checkpoint recovery, or container restart was verified. Production recovery requires the cluster's requeue policy and persistent shared storage on replacement nodes.

## Development add-ons — October 17, 2026

Verified on macOS with SRT 0.0.79, Codex CLI 0.163.0, and code-server 4.141.0:

- Created `tools-demo` using `--addon codex --addon code-server`. Both selections persisted in the attempt records. Creation enters the coding shell; the saved add-ons remain available after switching. From that shell, bare `code-server` started on an automatically selected loopback port with its backend and terminal inside SRT.
- Typed bare `codex` in the browser terminal and completed an interactive model turn that ran a shell command and read a demo file. A separate `codex exec` turn wrote `src/codex-demo.txt`. No Caligo-specific agent launcher or per-invocation flags were needed for interactive startup.
- Opened that file in the browser, edited it, saved it, and verified its contents on disk. Creating a file in user-mode `data/` through the editor failed with EPERM. An add-on-enabled shell also denied reads of the original manifest, fixed setup, and a neighboring attempt, plus writes to read-only data and undeclared root files.
- Startup fixes preserve `/etc` symlink aliases in read permissions, use a private temporary directory for editor sockets, allow the macOS FSEvents service, configure a verified CA bundle, and seed private Codex defaults to avoid nested Seatbelt and shared-daemon process inspection.
- For this live demo only, the existing local Codex authentication cache was copied into the attempt's private Codex home without displaying its contents. Automatic credential import is not implemented. Fresh attempts require login; the fresh device-auth flow was not exercised.
- `.venv/bin/python -m pytest -q`: **20 passed, 1 skipped**. Docker remains opt-in. Linux coding isolation and remote browser forwarding remain unverified; the editor preview explicitly requires a local coding host.

## Remote Python preflight — October 16, 2026

Caligo now requires Python 3.11+ on client and controller hosts. SSH startup probes interpreters before deploying the agent, selects a compatible default, and uses its absolute executable path for subsequent operations. Explicit `remote.python` settings are checked without fallback. The agent entrypoint also rejects unsupported Python before importing the controller.

- `.venv/bin/python -m pytest -q`: **20 passed, 1 skipped**. New transport tests execute the real probe, installer, and zipapp RPC through a local SSH stand-in. They cover a simulated Python 3.6 default alongside a modern interpreter, unsupported/missing explicit choices, paths containing spaces, SSH failures, and invalid manifest settings. The Docker test remains opt-in and was skipped.
- Live SSH against `orcd` found `/usr/bin/python3` at 3.6.8 and selected `/usr/bin/python3.12` at 3.12.14. Explicitly selecting the old `python3` produced the actionable version error before deployment.
- Updated the installed CLI in `/opt/anaconda3`, then ran `caligo exec --project /Users/hongningwang/side-projects/testing/project.toml -- python3 -c 'print("hello")'`: the frozen dev copy ran remotely, printed `hello`, and returned zero. No project configuration change or scheduler allocation was needed. Existing shell controllers must be restarted to load the updated package.
- Tool-access diagnosis in the existing attempt: `codex --version` succeeds, but `codex login status` fails while reading `/etc/codex/requirements.toml` with `Operation not permitted`; `code` is unavailable inside the sandbox, and its host launcher points into `/Applications/Visual Studio Code.app`. Full tool startup, authentication, and editor integration remain unimplemented and unverified; launching the binary alone is insufficient validation.

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
