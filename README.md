# Caligo

Caligo runs experiments through **human** and **agent** attempts. Humans get an interactive environment with normal tools and credentials. Agents get a persistent containerized backend that streams output and accepts steering. Both modes preserve the project's write permissions.

Projects contain attempts; attempts can submit many frozen runs. Agent attempts also have persistent conversation threads. Workspaces, conversation history, and run evidence survive viewer disconnects.

## Install

```sh
uv venv --python 3.12
uv pip install -e .
source .venv/bin/activate
```

Caligo requires Python 3.11+ on client and controller hosts, Git, and Bash. Human write protection uses macOS's built-in `sandbox-exec` or Linux Bubblewrap (`bwrap`). There is no unrestricted fallback when write enforcement is unavailable. Host tools, home directories, credentials, and networking otherwise remain available to human shells.

Agent coding requires a running Docker daemon on the coding host. Build the supplied image there:

```sh
docker build -t caligo-agent:local -f caligo/Agent.Dockerfile caligo
```

The image includes Python, Git, Codex, and the Claude Agent SDK. The host does not need either agent installed. Set `[agent].image` in the manifest. Custom images must provide the same tools; their backend versions must support the documented interfaces.

Dev and experiment hosts need the declared execution runtime, Docker or Apptainer/Singularity. Experiments use Slurm. Requeueable dev commands also use Slurm.

## Human attempts

```sh
caligo init dropout-study
cd dropout-study
# Prepare files and configure all three locations in project.toml.
caligo validate
caligo create baseline --mode human
```

Creation opens an interactive shell in the executable directory without allocating an experiment job. Inside it:

```sh
git status
# Edit code with installed tools.
caligo exec -- python3 check.py
caligo run
caligo create higher-dropout --mode human
caligo switch baseline
exit
```

`caligo list` lists this project's attempts. Names are unique and existing attempts are never overwritten. Switching opens a fresh shell with persistent files. Each shell retains its own attempt association, even when other terminals select different attempts.

Fork an attempt to try a variation from its current work, or delete an accidental attempt:

```sh
caligo fork baseline higher-dropout
caligo switch higher-dropout
caligo delete accidental-attempt
# For scripts: caligo delete accidental-attempt --yes
```

Fork copies current working files (including dirty, untracked, and ignored files), file permissions, and independent Git history/index. It keeps the source's mode, add-ons, Git branch, pinned configuration, and evaluator revision, even if the project's current files have changed. The new attempt is selected but no shell or agent starts automatically. Inside an existing shell, that shell retains its attempt; use `switch` to enter the fork. Finish editing before forking; concurrent file changes cause an error.

Agent forks start with fresh conversation storage and no copied agent home or outputs. Stop the source agent and wait for `STOPPED` before forking, then use `caligo agent start NEW_NAME --goal TEXT` on the fork.

Deletion asks for confirmation; `--yes` skips the prompt. Exit an attempt's open shells and stop its agent before deleting it. Deletion removes working files, private Git metadata, agent home, and coding outputs. Submitted jobs, frozen inputs, logs, artifacts, evaluation results, and conversation event history are retained. Jobs continue until explicitly cancelled. A deleted name can be reused. Deleting the selected attempt selects the most recently created remaining attempt; deleting the last one leaves an empty project where run commands such as `status` and `artifacts` still work.

Bare `caligo` and `caligo --help` show commands grouped into Attempts, Experiments, Agents, and Project & interface. Use `caligo help fork` or `caligo fork --help` for command options. `--project PATH` works before or after commands that select a project.

Agent controls are bundled under `caligo agent`: use `caligo agent` to see them, or `caligo help agent start` for a particular command. The old flat agent commands remain accepted as hidden compatibility aliases. Attempt management and experiment commands stay at the top level.

A human shell can read the normal host environment and use ordinary credentials. Its write policy protects the attempt's readable areas, existing read-only files, and workspace root. It also protects prepared evaluator files. Existing read-only files cannot be replaced through an editor's atomic save. Tools and editor servers launched from the shell inherit the boundary.

File permissions and executable bits are preserved during checkout creation and transfer. Each attempt has independent Git metadata outside its declared working files. Ordinary Git commands work through `GIT_DIR` and `GIT_WORK_TREE`; commits and pushes remain explicit. No source repository history or origin remote is imported automatically.

Host owners can deliberately administer their own filesystem permissions outside a session. Caligo protects collaboration through independent workspaces, protected prepared evaluators, and pinned revisions; it does not claim to restrict a hostile host administrator.

Optional `--addon codex` and `--addon code-server` check for installed human tools. They use your ordinary settings and credentials. Tools do not require add-on registration to run inside a human shell.

## Agent attempts and steering

Configure the agent image and explicitly forwarded environment variable names:

```toml
[agent]
image = "caligo-agent:local"
env = ["OPENAI_API_KEY"]
# model = "..."
continuation_delay = 2
```

Set credential values in the coding-host environment before the service starts; do not put them in the manifest. For Claude, use `ANTHROPIC_API_KEY` or another credential supported by the Claude SDK. Host login sessions are not imported. To rotate service environment credentials, stop the service and start it from an environment containing the new values.

```sh
caligo create search --mode agent --backend codex --goal "Improve validation accuracy"
# Creation returns immediately after starting a persistent thread.
caligo agent attach search
# Ctrl-C detaches; the agent keeps running.
caligo agent steer search "Focus on regularization next"
caligo agent steer search --delivery steer "Keep the current change small" # Codex active steering
caligo agent interrupt search "Investigate possible data leakage first"
caligo agent pause search
caligo agent resume search
caligo agent stop search
```

Use `--backend claude` for Claude Code. Ordinary steering is queued for the next turn; Codex additionally supports active steering. Claude supports queued input and interruption. Unsupported controls return an error. Interruption may wait for the backend to finish stopping its tools; resume once the pause has been applied.

Agents review their objective against changes, checks, and experiment results before ending a turn. `caligo review complete "evidence"` finishes the thread and stops its container after the turn ends. Completion requires no outstanding experiments or queued instructions. `review wait "reason"` waits for experiment results without repeated model calls; `review blocked "help needed"` asks for human input. `review continue "next action"` allows a continuous thread to keep working. Reviews and completion evidence are saved in the conversation event history.

Continuous threads request a review if the agent omits one, then pause if it still does not provide a decision. After 20 automatic turns they pause for a human progress check; `resume` allows another 20. Sending an instruction also resets this count. `--once` disables automatic continuation; the thread stays available for instructions and experiment results unless the agent explicitly completes it. Transient runtime disconnects retry with backoff, up to five restarts. Other runtime/backend errors become visible in the thread state and event stream. Correct the cause and use `resume`, or start another thread after completion:

```sh
caligo agent start search --backend codex --goal "Try another approach" --once
caligo agent threads search
caligo agent attach search --json
caligo agent attach search --after 123 --no-follow
caligo agent steer search --id instruction-42 "Run the comparison"
```

Message IDs support idempotent retries. The same ID with different content is rejected. After a crash, an instruction whose delivery cannot be confirmed is recorded as `DELIVERY_UNKNOWN` and is not blindly replayed.

One agent thread at a time can write an attempt. Stopping preserves working files and conversation storage. Backend thread/session IDs are saved separately from Caligo thread IDs and restored on runtime restart. Conversation state is private to the attempt.

The agent and its tools run as an unprivileged user inside Docker with a read-only root filesystem, restricted mounts, dropped capabilities, and outbound networking. Host homes, SSH credentials, other attempts, evaluators, and Docker sockets are absent. The coding container is briefly paused during snapshots to prevent concurrent path replacement.

Inside the container, `caligo exec`, `run`, `status`, `logs`, `artifacts`, `cancel`, and `publish` use an authenticated broker scoped to that attempt. Agents cannot create or switch attempts. Agent dev commands always run in containers too. They cannot escape isolation through a native dev-host command. Operator-side `exec` and `run` use that same persistent thread service.

## Browser interface

```sh
caligo ui
```

Create agents, follow their work, send instructions, and pause or resume them in your browser. Experiments has run status, logs, and downloads. Closing the browser leaves agents and submitted jobs running. The UI uses the configured local or SSH coding host.

Choose a model when creating an agent or thread. The dropdown lists models previously used with that backend. Choose Default for the project or backend default, or Other to enter a model ID. The selection stays with the thread across restarts.

Use `--project PATH` to select a manifest, `--port PORT` for a fixed address, or `--no-open` to print the URL. The frontend uses assistant-ui's prebuilt chat components with a ChatGPT-style layout and connects to Caligo's existing service. Send messages normally; while the agent is working, choose Send now, After this response, or Interrupt and send. The stop button pauses its response. Sending to a paused agent resumes it. From a source checkout, run `pnpm --dir ui install --frozen-lockfile` and `pnpm --dir ui build` first. Built wheels include the static UI and require only Python to serve it. See [ui/README.md](ui/README.md) for frontend development.

## Service API

The service runs on the coding host independently of viewers:

```sh
caligo agent service start search
caligo agent service status search
caligo agent service stop search
```

Stopping the service pauses its active threads. A subsequent `resume` starts the service if necessary and resumes the saved conversation. After an abrupt service crash, reconnecting starts recovery and reconciles orphaned containers. Configure OS service supervision separately if startup at host boot is required.

Service state defaults to `~/.local/state/caligo/`. Set `CALIGO_STATE_DIR` to a persistent **local disk** path on hosts with network-mounted homes. SQLite WAL and service control files must not be placed on NFS. The service's private directory contains `address.json`, a bearer `token`, `threads.sqlite3`, and its log/configuration. Its HTTP API listens on loopback; remote UI clients can use SSH forwarding.

The CLI and browser interface use these authenticated endpoints:

| Method | Path | Operation |
| --- | --- | --- |
| GET / POST | `/threads` | List or create threads |
| GET | `/threads/ID` | State and current event cursor |
| GET | `/threads/ID/events?after=N` | Replay JSON events or follow SSE |
| POST | `/threads/ID/messages` | `{text, delivery, id?, author?}` |
| POST | `/threads/ID/interrupt` | Interrupt; optional `{text}` redirects |
| POST | `/threads/ID/pause`, `/resume`, `/stop` | Lifecycle controls |
| POST | `/threads/ID/run` | Submit `{script?}` |
| POST | `/threads/ID/exec` | Execute `{argv}`; output enters event stream |
| GET | `/threads/ID/runs` | List the thread's runs |
| GET | `/threads/ID/runs/RUN_ID`, `/threads/ID/runs/RUN_ID/logs` | Run status or logs |
| POST | `/threads/ID/runs/RUN_ID/cancel` | Cancel a submitted job |

Send `Authorization: Bearer TOKEN`. SSE clients send `Accept: text/event-stream` and may resume using `Last-Event-ID`. Events include schema version 1, sequence IDs, timestamps, an origin (`caligo` or `backend`), normalized progress, native backend events, dev output, and run updates. The current token authorizes the project service; author labels are recorded labels rather than an independent multi-user identity system.

The container-facing broker has a separate token and only attempt-scoped operations. It uses a TCP bridge for Docker Desktop compatibility and never grants access to the external thread administration API.

## Project configuration and revisions

```toml
name = "dropout-study"
cpu = 8
# gpu = "h200:4"
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
env = ["OPENAI_API_KEY"]

[remote]
# root = "/shared/path/caligo"
# python = "/path/to/python3.12"

[scheduler]
type = "slurm"
requeue = true
partition = "your-partition"
memory = "64G"
time = "01:00:00"
# account = "your-account"
# setup = "module load apptainer"

[inputs]
data = "s3://datasets/versioned-inputs/"
reference = "/local/reference"
```

Every location must be explicit. Remote controllers require Python 3.11+; Caligo probes compatible interpreters or uses `[remote].python`. Authenticate ordinary SSH first; `CALIGO_SSH_CONFIG` selects another SSH config.

A persistent remote agent's dev and experiment SSH aliases must be reachable from the **coding host**. For remote coding, client-local dev/experiment locations are rejected because they cannot survive client disconnection.

New attempts pin a revision of the current manifest, declared initial files, and evaluator. Editing these through Git and creating another attempt produces a new revision within the project. Existing attempts retain their earlier files and evaluator. Prepared evaluator changes are checked against their hash before execution. Docker uses locally cached images when present; pull or rebuild tags explicitly to update them. Resolved image IDs are recorded, and an agent attempt retains its pinned image across runtime restarts. Bucket inputs are frozen at first import of a revision; use versioned URIs or a changed input definition to intentionally import another dataset.

Local project state lives in `.caligo/`. Default remote project storage is `~/.local/share/caligo/`. State version 3 introduces human/agent attempts; use `caligo migrate` to explicitly convert legacy attempts to human mode without deleting their edits. Existing run artifacts remain readable.

Each new attempt includes a short, read-only `Agent.MD` generated from its TOML revision: commands, folder permissions, and a checkpointing reminder. Both agent backends receive its contents automatically.

## Dev commands, evaluation, and artifacts

`caligo exec -- COMMAND` copies current declared files to the dev location, streams output, and returns the command's exit code. Human dev commands use the host environment under write protection; agent dev commands use the declared experiment container. The copy is separate even when coding and dev are both local. Fixed evaluators are excluded, and edits never synchronize back. Retained copies consume disk until removed.

`caligo run` uses `execute`; `caligo run executable/other.py` selects another script. It freezes dirty, untracked, and ignored files, submits to Slurm, and retains exact hashes and provenance. Participant code receives writable `CALIGO_OUTPUTS` and cannot access `/fixed`.

After successful participant execution, a separate evaluation container receives `/fixed`, a read-only workspace, read-only participant artifacts at `CALIGO_OUTPUTS`, and a writable `CALIGO_EVALUATION`. **Update evaluators to write metrics/results into `CALIGO_EVALUATION`.** Authoritative results live under the run's `evaluation/`; participant artifacts live under `artifacts/`. Artifact symlinks and special files are rejected before evaluation. Evidence downloads skip unsafe links while retaining ordinary logs and records.

```sh
caligo status RUN_ID
caligo logs RUN_ID
caligo artifacts RUN_ID ./downloads
caligo cancel RUN_ID
caligo publish RUN_ID
```

Failures retain available evidence. Artifact downloads use a fresh run directory and refuse to overwrite an existing destination. Bucket transfers/publication use rclone. Completed run results are delivered once to their originating agent thread; interrupting or stopping a thread does not cancel submitted jobs. Persistent run routes remain discoverable after the agent service stops and after selecting another project revision.

## Slurm requeueing

`scheduler.requeue = true` enables requeue eligibility for experiments and routes dev commands through Slurm. Both use the manifest's compute requests and scheduler setup. Coding does not allocate GPUs.

Slurm restarts the original command with the same run ID, working directory, checkpoint outputs, and appended logs. Your program saves and reloads checkpoints. Caligo preserves each execution under `executions/RESTART_COUNT/` and uses current scheduler evidence rather than stale outcomes. It does not resubmit after application failure or explicit cancellation. Cluster policy must permit requeueing; `--requeue` does not override it.

Controller records, execution copies, images, and checkpoint outputs must be on persistent shared storage accessible to replacement compute nodes. Bucket publication is an export rather than live checkpoint replication. Service SQLite state remains on the coding host's local disk.

## Verification

```sh
pytest -q
CALIGO_TEST_DOCKER=1 pytest -q
```

Keep tests focused on user-visible behavior: workspace creation and permissions, frozen runs and evaluation, agent communication and completion, host routing, storage, and checkpoint recovery. Avoid assertions about exact prose, listing order, private storage layouts, or policy thresholds. See [docs/verification.md](docs/verification.md) for observed coverage and limits.
