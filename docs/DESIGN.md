# Caligo

Caligo isolates research experiments and distributes jobs to Slurm clusters.
Human users are trusted. Keep the implementation simple and readable.

## Configuration

`caligo.toml` format. Dotted fields belong to the corresponding TOML tables.

| Field | Type | Meaning |
| --- | --- | --- |
| `base_image` | string | Research environment for dev and run jobs. |
| `coding_image` | string | Shared development image for human shells and agents; `caligo-coding:local` builds the bundled image. |
| `coding` | string | `local` or an SSH config name; hosts coding sandboxes. |
| `writable` | array of strings | Agent-editable repository directories; descendants inherit. |
| `logs` | string | Local directory, remote directory, or bucket URI. |
| `datasets` | optional table of strings | Dataset names mapped to absolute host paths; mounted read-only at `/datasets/<name>`. |
| `dev.hosts` | array of strings | SSH config names for dev jobs. |
| `dev.hardware.cpus` | positive integer | CPUs per dev job. |
| `dev.hardware.gpus` | nonnegative integer | GPUs per dev job; zero means CPU-only. |
| `dev.hardware.gpu_type` | optional string | Cluster-specific GPU type; omitted means any type. |
| `run.hosts` | array of strings | SSH config names for final evaluation jobs. |
| `run.command` | array of strings | Executable followed by arguments for final evaluation. |
| `run.hardware.cpus` | positive integer | CPUs per run job. |
| `run.hardware.gpus` | nonnegative integer | GPUs per run job; zero means CPU-only. |
| `run.hardware.gpu_type` | optional string | Cluster-specific GPU type; omitted means any type. |

Dev and run also accept optional `partitions` (array of strings), `account`, `qos`,
and `time` (Slurm strings). Resource counts apply to one node and one task.

The coding image supplies Bash, Vim, tmux, Git, ripgrep, Python, Node, Codex, and
Claude Code. Tools are installed once in the image; credentials and sessions stay
private to each experiment. Docker shares image layers; Apptainer coding and job
images use one cache per host, outside experiment directories. Dev/run use the
separate base image. Record image digests and require matching host architectures.

## Experiments and sandboxing

- The base repository is the `admin` experiment. Each new experiment uses a Git
  worktree and records its admin commit and configuration as a fixed baseline.
- Each experiment has its own unprivileged container, home, caches, temporary
  files, outputs, and process namespace. Other experiments and unrelated host
  files are hidden. Shared datasets must be explicitly mounted read-only.
- Mount the repository read-only, then mount approved `writable` directories over
  their paths. Human-maintained core code, root files, evaluation files, and policy
  stay read-only; reject writable paths that overlap protected files or escape
  the repository.
- Arbitrary experiment code may execute. There is no `executable` directory list.
  Runtime outputs and caches live outside the protected repository tree.
- The external Caligo controller owns SSH credentials, shared Git metadata, and
  job submission. Sandboxes receive no host home, privileged access, or container
  management socket. Requests to the controller are scoped to one experiment.
- Dev and run capture a stable snapshot, including uncommitted edits, and compare
  protected paths against the baseline, including additions, deletions, and
  symlinks. Reject violations; transfer exactly the validated snapshot to a unique
  scratch directory and apply the same isolation remotely.

Containers avoid a separate VM per experiment. On macOS, the container runtime may
use a shared Linux VM. Remote targets must support the required container isolation.

## Job distribution

- All dev and run targets use Slurm. Submit only to partitions matching the
  requested hardware; use one submission with multiple eligible partitions per
  cluster where possible.
- Across clusters, candidate jobs must claim a single winner through the
  controller before executing. Cancel losing submissions after a winner claims
  its allocation. Submission acceptance alone does not select a winner.
- Stream stdout and stderr; retain the snapshot identity, configuration, image
  digest, job IDs, exit status, and outputs with the logs.

## Agents

- Each experiment has a goal and persistent conversation, following
  [OpenResearch's session/worktree model](https://github.com/alphaXiv/OpenResearch/blob/main/SYSTEM_PROMPT.md).
  Humans set the research direction, maintain core code, and integrate results.
  Agents edit only approved experiment files, run jobs, and report evidence.
- `caligo goal <experiment> "<task>"` sets or revises the goal. Include constraints
  and stopping conditions, such as "compare three learning rates and report the
  best validation score." Caligo stores the goal outside the repository and
  delivers revisions to the agent as conversation messages; changing a file alone
  is insufficient. Record the current goal with each submitted job.
- `caligo attach <experiment> --agent codex|claude` opens the agent's native
  interactive terminal inside the sandbox. The goal becomes its initial task.
  Chat there to ask questions, inspect results, interrupt work, or change direction.
  Save steering messages alongside the goal as part of the experiment history.
  Start with this terminal experience; a custom chat UI is optional later.
- `caligo start <experiment> --agent codex|claude` starts the agent detached.
  Setting a goal alone does not start it. Attach later to interact; detach with
  Ctrl-b d. `caligo stop <experiment>` stops the agent without cancelling jobs.
- Reattaching reconnects to the existing session; it does not start a second agent.
  Detaching leaves the session running. After a process exits, resume its saved
  conversation with the current goal. Stopping an agent does not cancel Slurm jobs;
  cancel those explicitly with `caligo cancel <job-id>`.
- Caligo supplies the goal, permitted paths, available commands, and job results;
  the CLI supplies the agent loop and conversation. Agents may iterate within the
  human-defined task, but cannot change core code, evaluation, policy, or the admin
  baseline. Humans review and integrate proposed changes.
- Use the user's authenticated CLI account; Caligo requires no API keys. Keep
  credentials and separate session state outside submitted snapshots. Unattended
  tasks may use the CLI's structured output. Agents outside the sandbox are outside
  Caligo's isolation boundary.
- Keep task prompts concise: objective, constraints, and verification steps
  ([Codex prompting guide](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide)).

## Commands

```text
caligo init                                  Create starter config; initialize Git if needed.
caligo create experiment <name>               Create a worktree from the admin baseline.
caligo goal <experiment> "<task>"              Set or revise its goal; notify its agent.
caligo auth <experiment> --agent codex|claude  Authenticate inside its private home.
caligo start <experiment> --agent codex|claude Start working without attaching.
caligo stop <experiment>                      Stop the agent; leave Slurm jobs running.
caligo attach <experiment>                    Enter its sandbox shell; copy vimrc into its home.
caligo attach <experiment> --agent codex       Start or reconnect to its Codex conversation.
caligo attach <experiment> --agent claude      Start or reconnect to its Claude Code conversation.
caligo dev <command> [args...]                Snapshot and submit using dev hosts/hardware; stream results.
caligo run                                   Snapshot and submit run.command using run hosts/hardware.
caligo jobs                                  List the current experiment's jobs and statuses.
caligo logs <job-id>                          Follow a job's output.
caligo cancel <job-id>                        Cancel a queued or running job.
```

Dev and run operate on the current experiment. Agents invoke the same commands
through the experiment-scoped controller interface.

Example workflow:

```sh
caligo create experiment lr-sweep
caligo goal lr-sweep "Compare three learning rates; report validation scores and stop."
caligo attach lr-sweep --agent codex
# Continue chatting in Codex: "Compare two learning rates instead; use the smaller model."
# Detach and reattach later to continue the same conversation.
```
