# Caligo: functional requirements

Caligo provides human and agent attempts for reproducible experiments. Project authors prepare code, data, evaluators, and configuration through ordinary Git workflows. Attempts perform work against a pinned project revision. There is no supervisor mode.

## Projects, revisions, attempts, threads, and runs

- A project is selected by its `project.toml` directory and contains uniquely named attempts.
- A revision freezes the manifest, initial declared files, permissions, and evaluator. Creating an attempt from changed source files or configuration creates a new revision within the project. Existing attempts keep their original revision.
- An attempt owns independent persistent working files and Git metadata. Its mode is `human` or `agent`.
- A thread owns an agent conversation, objective, backend identity, lifecycle, and steering history. A thread survives viewer disconnects and can resume native backend conversation state after a runtime restart.
- A turn is one interval of backend activity. Ending a turn does not end the thread.
- A run is a frozen experiment submission. Record its submitting attempt, revision, Git state, actual input hashes, and originating thread and turn when available.
- Allow only one active agent thread to write an attempt. Concurrent collaborators use separate attempts. Stopping a thread preserves its workspace and conversation evidence.

```sh
caligo create baseline --mode human
caligo create search --mode agent --backend codex --goal "Improve validation accuracy"
caligo agent attach search
caligo agent steer search "Focus on regularization"
caligo agent interrupt search "Investigate data leakage first"
caligo agent pause search
caligo agent resume search
caligo agent stop search
```

## Human mode and write permissions

Human mode is interactive and uses the host's installed tools, normal home directory, credentials, and Git configuration. Launch in the executable directory. Exiting the shell preserves working files. `caligo switch NAME` enters a human attempt or attaches to an agent thread.

**Human mode must enforce write permissions.** Access to ordinary tools does not grant permission to modify protected project files.

- Declared editable and executable areas permit writes subject to the original file permissions. Preserve existing read-only files and executable bits; never recursively grant write access to make tools work.
- Declared readable areas remain read-only, including their directory entries. Prevent deletion, renaming, and atomic editor replacement of protected files and directories.
- Prevent creation of undeclared entries at the workspace root.
- Preserve and reapply permissions during checkout creation, copying, snapshotting, and transfers. Git does not record arbitrary read/write permissions.
- Apply a write-only OS policy to human shells and their descendants: macOS `sandbox-exec`, or Linux Bubblewrap with ordinary host access and protected read-only mounts. Tools retain host reads, credentials, and networking. Missing enforcement support is an error.
- Protect prepared evaluators outside writable checkouts. Use protected filesystem permissions and macOS ACLs where available. Project owners with host access can deliberately administer permissions; this does not claim to defend against a hostile host administrator.
- Editors and tool servers launched from the human shell inherit its write boundary. Existing unrelated editor processes do not inherit that shell policy.
- Ordinary Git operations remain explicit. Keep independent Git metadata outside declared content, so working-file snapshots never include Git administration files or undeclared source history.

Optional human `--addon codex` or `--addon code-server` checks for installed tools; these use the normal human environment. Agent backends are configured through the agent image rather than human add-ons.

## Agent container boundary

The agent backend and every local tool subprocess run inside a real container. Implement Docker for the coding runtime. Do not fall back to a native host agent or the former SRT coding shell.

- Use an unprivileged UID/GID, read-only root filesystem, dropped capabilities, no-new-privileges, a process limit, and writable temporary storage.
- Mount only the attempt workspace, private Git metadata, private agent home, coding outputs, trusted Caligo code, and the scoped broker client. Mount readable inputs and originally protected files read-only.
- Keep the host home, source repository, project manifest, evaluator, service state, other attempts, SSH agent, and container engine socket outside the container.
- Permit outbound networking. Do not use host networking.
- Supply only explicitly named agent environment variables. Do not import host sessions or credentials automatically, persist credential values in Caligo configuration, or put credential values into container command arguments.
- Resolve the agent image to an immutable image ID and retain it across thread runtime restarts.
- Persist native backend conversation state in the attempt's private agent home.
- Briefly pause the coding container while the trusted controller snapshots agent-owned files. Resume it before transfer or execution. Collect agent-controlled Git metadata inside a separate read-only container, never by executing its Git configuration on the host.
- Coding does not allocate experiment GPUs.

Use a session-scoped authenticated broker to request development commands, experiment submissions, logs, status, artifacts, publication, and cancellation. Bind it to one attempt and its runs. Deny attempt creation, switching, and access to neighboring attempts. Keep scheduling configuration and host credentials outside the container. The Docker Desktop bridge must work across its VM boundary; do not assume an arbitrary host Unix socket can be used inside a Linux container.

## Persistent service, streaming, and steering

The coding-host service owns agent containers independently of the CLI or UI. Store durable thread state, an ordered steering inbox, and an append-only event journal in SQLite on local disk. `CALIGO_STATE_DIR` selects that disk; sites with network-mounted homes must configure it explicitly.

- Keep the external HTTP API on loopback and require its bearer token. Remote clients use SSH transport/forwarding. The separate container-facing broker grants only attempt-scoped operations.
- Expose thread creation, listing, history, messaging, interruption, pause/resume, stop, and run/dev execution through the same API used by CLI clients.
- Stream events through SSE with monotonically increasing sequence IDs and replay using `after` or `Last-Event-ID`. Persist normalized progress, native backend events, tool output, dev output, run state, and lifecycle changes.
- Viewer disconnects must not terminate the agent. Ctrl-C in `attach` detaches and reports a replay cursor.
- Record steering text, author label, requested delivery mode, and delivery status. Reusing a message ID with identical content is idempotent; conflicting reuse is an error.
- Queue ordinary instructions for the next turn. Explicit active steering may bypass queued next-turn messages when the backend supports it. Report unsupported capabilities instead of silently changing their meaning.
- Interruption stops the active backend turn. An optional new instruction redirects the next turn. Pause interrupts and disables continuation; resume restores it. Stop tears down the container while preserving files and evidence.
- Already submitted experiment jobs are independent of thread interruption and stopping. Cancel them explicitly with their run IDs.
- Continuous threads continue their objective at idle turn boundaries. `--once` disables automatic objective continuation while preserving an available, steerable thread. Observe submitted runs and deliver terminal results to their originating thread once.
- Backend or runtime failures must be visible. Retry transient runtime disconnects with backoff up to five restarts, retaining the backend identity. Preserve ambiguous message delivery as `DELIVERY_UNKNOWN` after crashes instead of replaying it blindly.
- Reconcile orphaned containers before recovering a thread. An intentional service shutdown pauses active threads; an abrupt restart recovers active threads from durable state. `service start` starts the detached service; OS boot supervision is a deployment concern.
- Keep transport and backend details out of the UI. UI clients consume the same commands and event stream as the CLI. The current bearer token authorizes the local project service; author labels are not a multi-user identity system.

## Native agent backends

- Codex uses `codex app-server` with persistent threads, streamed notifications, native active steering, interruption, and thread resume.
- Claude Code uses `ClaudeSDKClient` from the Claude Agent SDK with persistent sessions, streamed responses, queued input, and interruption. Drain the interrupted result before starting another query. Do not advertise Codex-style active steering for this adapter.
- Delegate conversation context and tool execution to these backends. Caligo owns container isolation, experiment operations, event persistence, and lifecycle controls.
- Keep Caligo thread IDs separate from backend conversation IDs. Persist both IDs and expose capabilities to clients.
- Run with unattended backend approval settings inside the outer container boundary. Unexpected interactive requests produce a visible blocked state.
- `caligo fork SOURCE NEW` creates a separate attempt from the source's current working files and independent Git metadata/index, retaining its pinned revision and mode. Dirty, untracked, and ignored files and permissions survive. Agent forks require a stopped source and begin with fresh conversation storage; they never share a writable workspace or native backend session.
- `caligo delete NAME` confirms before removing an attempt's working files and private state. Open shells and active agents prevent deletion. Submitted runs, routing, and conversation event history survive; deleting an attempt does not cancel experiments. Names may be reused after deletion.

## Configuration and host routing

Require `locations.coding`, `locations.dev`, and `locations.experiments`, each explicitly `local` or an SSH alias. Do not infer one location from another. Human tools use their configured coding host; expensive work uses dev or experiments explicitly.

```toml
name = "dropout-study"
cpu = 8
image = "my-experiment-image:latest"
editable = ["src", "configs"]
readable = ["data", "reference"]
executable = "executable"
execute = "executable/train.py"
fixed = "fixed"
eval = "eval.py"
outputs = "results"
runtime = "apptainer"

[locations]
coding = "local"
dev = "dev-host"
experiments = "cluster"

[agent]
image = "caligo-agent:local"
env = ["OPENAI_API_KEY"] # Names only; values come from the coding-host service environment
# model = "..."
continuation_delay = 2
```

The detached service runs on the coding host. For remote coding, development and experiment SSH aliases must be reachable from that host. Client-local execution locations cannot remain available after the client disconnects; reject that remote-agent configuration.

Keep compute, partitions, accounts, paths, and setup commands configurable. Require Python 3.11+ on client and controller hosts, with existing compatible-interpreter discovery and revision-specific zipapp deployment. Include agent runtime resources in installed packages.

## Development and experiments

`caligo exec -- COMMAND` freezes current declared files into a distinct dev copy, streams output, returns its exit code, and retains its outputs. Never synchronize dev edits over coding files. Exclude the evaluator from dev.

- Human dev commands use the host environment under its write policy.
- Agent dev commands use the configured experiment container image and runtime, including scheduled dev commands. There is no native-host command escape through the broker.
- `caligo run` freezes current declared content, including dirty, untracked, and ignored files, and submits it to Slurm at the experiment location. Later changes cannot alter submitted inputs.
- Participant execution receives its workspace and writable artifact outputs. It does not receive `/fixed` or `CALIGO_FIXED`.
- Evaluation runs separately with the evaluator and participant artifacts mounted read-only. Only its evaluation result directory is writable. Evaluators read `CALIGO_OUTPUTS` and write `CALIGO_EVALUATION`; participant-written files cannot impersonate authoritative evaluation results.
- Preserve logs, outcomes, hashes, and available artifacts after failures. Reject artifact links before evaluation and skip unsafe links in evidence downloads. Keep working files, controller records, evaluation results, and artifact exports separate. Discover persistent run routes without requiring the originating service to remain running, and use each run's original host settings.
- Support filesystem and bucket inputs/outputs through existing rclone transfers. Freeze bucket inputs on first import of a revision; use versioned input URIs or a new input definition to intentionally change external datasets.

## Preemption

Preserve the existing Slurm requeue implementation:

- `scheduler.requeue` defaults to false. When true, experiments and dev jobs are requeue-eligible using the declared CPU/GPU and scheduler configuration.
- Application code saves and reloads checkpoints. Restart the same command, working directory, and outputs directory; append logs and preserve writable working files.
- Keep the same Caligo run ID and Slurm job ID across requeues and retain every execution outcome.
- Prefer current queued/running scheduler evidence over stale outcomes from previous executions. Do not resubmit after application failures or explicit cancellation.
- Compute-node files and checkpoints must use persistent shared storage. Bucket publication is an export, not live checkpoint replication.

## Compatibility and verification

Project state uses version 3. Keep legacy run evidence readable. `caligo migrate` explicitly converts legacy attempts to human mode and reapplies write protection without deleting their working files. New project revisions do not rewrite old attempts.

Verify observable behavior with real shells and containers, plus a deterministic subprocess backend for repeatable lifecycle checks. Cover protected writes and atomic replacement, normal human host access, snapshot permissions, frozen revisions, native backend startup, scoped broker access, containerized dev, output replay, steering during active tools, interruption, idempotency, one writer per attempt, recovery, separate evaluation, host routing, and existing requeue/checkpoint behavior. Report live inference, SSH, Linux, and Slurm coverage accurately.
