# Caligo

Human-Written
---
Mostly vibecoded at this point, but driven by my goals and designs. The code quality of this codebase will improve over time.

The TLDR is that ML is a very collaborative effort between humans and agents. However, rogue agents and human errors can easily lead to evaluation drift, reward hacking and method drift. Also  Slurm is weird. This repo is my attempt at starting to solve these issues for my personal use. Basically,

- Humans write a base template for a project, with sufficient flexibility and capability to test and evaluate different methods. This step is up to human ingenuity. 
- Each experiment is a git worktree. Agents or humans can attach to a worktree, which puts them inside a Docker container with basic shell tools. Agent sessions are essentially claude/codex /goal loops maintained inside tmux sessions, and can be steered through the regular cli.
- Eval files are fixed as read only. Only certain files are modifiable to create more ``controlled'' experiments.
- Slurm commands are abstracted away into dev (arbitrary execution) and run (run a fixed evaluation files).


AI Written
---

Isolated research experiments, persistent Codex/Claude sessions, and Slurm jobs.
Python 3.11+, Git, Docker, and tmux are required locally. Slurm targets need
Apptainer and Python 3.6+. See [the configuration format](docs/DESIGN.md#configuration).

## Install

```sh
uv tool install .
# Or: pip install .
```

## Use

From your research repository:

```sh
caligo init
# Edit caligo.toml: base image, writable directories, SSH hosts, CPU/GPU requests.
git add .
git commit -m "Experiment baseline"
caligo login codex
caligo create experiment trial-1
caligo goal trial-1 "Compare three learning rates, report validation scores, then stop."
caligo start trial-1 --agent codex
caligo attach trial-1
```

Detach with **Ctrl-b d**; the agent keeps running. Reattach to the same native CLI
conversation to steer it. Caligo sets the objective using the native `/goal`
command in both Codex and Claude Code. `caligo goal trial-1 "revised task"` sends
a replacement `/goal` to a running agent. `caligo stop trial-1` stops the agent,
leaving submitted jobs alone. Use `caligo login claude` and `--agent claude` for
Claude Code.

Log in once per project with `caligo login codex` or `caligo login claude`.
Credentials stay under `.caligo/auth/home` on the coding host, outside worktrees
and submitted snapshots. Each experiment keeps its own conversation, settings,
and home. Codex sessions share the credential file so token refreshes stay in sync;
Claude sessions receive a private credential copy at startup. After logging in
again, restart agents to use the updated account. `caligo login codex --import`
imports file-based credentials from the coding host's home instead of opening
the login flow. `caligo auth --agent codex` and the older
`caligo auth trial-1 --agent codex` spelling also save a project login.

`coding_image` is shared by human shells and agents. The default `caligo-coding:local`
builds automatically once from Node's Debian slim image, with Bash, Vim, tmux, Git,
ripgrep, Python, Codex, and Claude Code. To build ahead of time: `caligo image build`.
Use `caligo attach trial-1 --shell` for human mode. Tools live in the image;
conversations live in each experiment's private home. Project login uses the same
image with an empty workspace and no datasets, so it can precede experiment creation.

`base_image` is the separate research environment for Slurm dev/run jobs; the
starter uses `python:3.12-slim`. Custom coding images need Python 3.11+, Bash, and
the agent CLIs you use. Core and evaluation code stay read-only; only configured
experiment directories are editable. Humans review and integrate results.

```sh
caligo -e trial-1 dev python3 experiments/test.py
caligo -e trial-1 run
caligo -e trial-1 jobs
caligo -e trial-1 logs JOB_ID
caligo -e trial-1 cancel JOB_ID
```

Use `--no-wait` before a dev command or after `run` to submit and return immediately.
`caligo logs JOB_ID` waits for completion, reporting state changes and a waiting
status every 30 seconds without output. Image preparation and Slurm queue waits
can take a long time; agents are instructed to keep waiting and continue from the
results. If a command tool times out, reconnect to the same job with
`caligo logs JOB_ID`. Ctrl-C disconnects the log follower without cancelling the job.
Inside the sandbox, commands automatically target its experiment. Outside it,
select `-e NAME` or work from the directory printed by `caligo path NAME`.
Job artifacts and provenance stay under `.caligo/experiments/NAME/jobs/JOB_ID`.
Logs are also copied to the configured destination: a local path, `SSH_ALIAS:/path`,
or `s3://bucket/prefix` (requires an authenticated AWS CLI on the controller).
Optional `datasets` maps names to absolute paths present on each host; they appear
read-only at `/datasets/NAME` and are not copied into job snapshots.
Set `coding_datasets = {}` before any TOML table to keep datasets on the Slurm
hosts when coding locally. A `coding_datasets` table can instead specify local
read-only mounts. If omitted, coding sessions use `datasets` as before.

Set `coding` to an SSH alias to host worktrees and agent sessions remotely. Caligo
mirrors committed admin history, deploys its Python package, and forwards commands
over SSH. The coding host needs Python 3.11+, Git, tmux, and Apptainer; Python from
a miniforge environment module is detected when necessary. Its SSH configuration
must resolve other execution hosts. The coding host itself needs no SSH hop.

Optional fields under `dev` and `run`: `partitions` (string array), `account`, `qos`,
and `time` (Slurm strings). Without `partitions`, Caligo considers all partitions
matching the requested hardware. Jobs use one node and one task with the requested
CPUs/GPUs. Scratch uses the target's `CALIGO_SCRATCH`, `~/orcd/scratch` if available,
or `~/.cache`. Coding and job images share a SIF cache per host, defaulting to
`~/orcd/scratch/caligo/images` or `~/.cache/caligo/images`; `CALIGO_IMAGE_CACHE`
overrides it. Image hashes are recorded. The bundled coding image builds with
Apptainer fakeroot on remote coding hosts; registry coding images are pulled.

Configuration requires both image fields. Commit configuration before creating
an experiment; existing experiments retain their recorded configuration.
If creation reports that `caligo.toml` is missing from the committed admin baseline,
run `git add caligo.toml` and `git commit -m "Add Caligo configuration"` from your
research repository, then retry creation. An untracked or staged file is not yet
part of the baseline.

## Verify

```sh
python3 -m unittest -v
CALIGO_TEST_DOCKER=1 python3 -m unittest -v tests.test_caligo.DockerIntegrationTest
python3 -m tests.integration_orcd orcd  # Opt-in: submits a short CPU job.
python3 -m tests.integration_coding orcd  # Opt-in: remote coding/session workflow.
```

Tests cover user workflows and isolation guarantees. Live tests require your own
SSH access; they do not call a model or need model credentials.

## License

[MIT](LICENSE).
