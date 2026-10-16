# Caligo

A project contains **attempts**. Each attempt has its own persistent coding workspace and independent Git metadata. An attempt can submit many **runs**.

The project directory selects the project. `caligo create`, `caligo list`, and `caligo switch` manage attempts inside it.

## Install

```sh
uv venv --python 3.12
uv pip install -e .
source .venv/bin/activate
npm install -g @anthropic-ai/sandbox-runtime@0.0.79
```

Coding hosts need Python 3.11+ for the CLI (remote workers support 3.9+), Git, Bash, Node 22.12+, ripgrep, and Sandbox Runtime (`srt`). macOS uses its built-in `sandbox-exec`; Linux needs Bubblewrap and the dependencies reported by `srt`. macOS coding shells have been verified; Linux coding isolation has not yet been verified.

Alternatively, install the same npm package under the active Python environment with `npm install --prefix "$VIRTUAL_ENV/caligo-tools" @anthropic-ai/sandbox-runtime@0.0.79`. Caligo also discovers that location. The local development install uses this option. Missing sandbox dependencies produce an error; Caligo does not fall back to an unrestricted shell.

## Start a project and make attempts

```sh
caligo init dropout-study
cd dropout-study
# Prepare your code/data and edit project.toml, including all three locations.
caligo validate
caligo create baseline --mode user
```

`create` reads the current project's manifest, creates the named attempt from the project's initial declared files, and immediately enters its coding shell in the executable directory. It does not contact dev or submit an experiment. Attempt names must be unique within the project. Existing attempts are never overwritten.

Inside the coding shell:

```sh
caligo list
# Project: dropout-study
# * baseline  (user)

# Edit files yourself, or start an installed coding agent:
codex

caligo exec -- python3 check.py   # Explicit command on the configured dev host
caligo run                      # Full scheduled experiment

caligo create higher-dropout --mode user
# Leaves baseline's shell and enters the new attempt.
caligo switch baseline
# Returns to baseline, with its edits preserved.
exit
```

`caligo list` marks the attempt belonging to this shell, even if another terminal has selected another attempt. Switching is handled by the outer project controller: the old coding shell exits and a new shell starts with the target attempt's access rules. Shell processes are not suspended or resumed; files persist. A new attempt starts from the initial project files, not another attempt's uncommitted edits.

After leaving, `caligo switch baseline` reopens that attempt. To use another project, leave the coding shell and change into that project's directory. On the host, `--project /path/to/project.toml` can select the project explicitly. Host `exec` and `run` use that project's most recently selected attempt.

## Workspace boundaries

Only declared editable, readable, and executable folders exist in the attempt workspace. The project manifest, fixed evaluator, original repository, controller records, other attempts, and Git control files are outside it.

| Area | User | Supervisor |
| --- | --- | --- |
| Editable folders | Read/write | Read/write |
| Readable folders | Read-only | Read/write |
| Executable folder | Read/write | Read/write |
| Undeclared root entries | Cannot create | Cannot create |
| Fixed evaluation and other attempts | Inaccessible | Inaccessible |

The coding shell and its child processes run under Sandbox Runtime. macOS enforces access restrictions rather than presenting a separate filesystem root. The goal is to protect neighboring attempts during ordinary coding, not defend against a hostile user. Standard installed tools remain usable; coding does not require a Docker image or VM. Shell network traffic goes through the runtime's proxies, with public destinations permitted.

Each attempt has an independent Git repository containing only declared content, with no origin remote pointing to the source project. `GIT_DIR` and `GIT_WORK_TREE` make ordinary Git commands work inside its shell. Commits and pushes remain explicit. Git control files, the attempt's private home, and coding outputs live outside its workspace.

An installed Codex CLI is available in the shell. Run `codex login --device-auth` to authenticate, then `codex`. Its per-attempt home persists across switching. Host credentials and sessions are not automatically imported.

## Project configuration

Prepare the files and manifest manually or use `caligo init` as a scaffold. Folder names are configurable.

```toml
name = "dropout-study"
cpu = 8
gpu = "h200:4"                    # Optional; full experiments only
image = "my-experiment-image:latest"
editable = ["src", "configs"]
readable = ["data", "reference"]
executable = "executable"
execute = "executable/train.py"   # Optional default experiment script
fixed = "fixed"
eval = "eval.sh"                 # Optional script inside fixed/
outputs = "results"
runtime = "apptainer"

[locations]
coding = "local"
dev = "dev-host"
experiments = "cluster"

[remote]
# root = "/shared/path/caligo"
# python = "python3"

[scheduler]
type = "slurm"
partition = "your-gpu-partition"
memory = "64G"
time = "01:00:00"
# account = "my-account"
# setup = "source /etc/profile && module load apptainer"

[inputs]
data = "s3://datasets/my-inputs/"
reference = "/local/reference"
```

Every location must be explicit: `local` or an SSH alias. Coding, dev, and experiments never inherit locations from one another. Authenticate ordinary SSH first; `CALIGO_SSH_CONFIG` selects a separate SSH config if needed. Remote coding shells additionally need SSH Unix-socket forwarding and `srt` installed on the coding host.

The project definition is frozen when its first attempt is created. Use a separate project directory for a different definition. There are no old command aliases or state migrations. Local state is under `.caligo/`; default remote storage is `~/.local/share/caligo/`.

## Dev commands and full experiments

`caligo exec -- COMMAND` snapshots the current declared files into a separate directory on the dev host, runs from its executable folder, streams stdout/stderr, and returns the command's exit code. It uses the dev host's installed dependencies and does not allocate a Slurm job. The copy is separate even when dev and coding both select `local`. Fixed evaluation is excluded. Edits never synchronize back to the coding attempt.

Dev copies and outputs are retained on the dev host; their location is printed, and execution records are kept under the project's `.caligo/` state. These retained copies consume disk until removed. Dev stdin is noninteractive; coding is the interactive shell.

`caligo run` uses the manifest's default script. `caligo run executable/other.py` selects another script. Full experiments freeze all declared files, including dirty, untracked, and ignored files, and submit to Slurm at the experiment location. The job executes the snapshot in the configured container and then runs the optional fixed evaluation. Later edits do not change a submitted run.

```sh
caligo status RUN_ID
caligo logs RUN_ID
caligo artifacts RUN_ID ./downloads
caligo cancel RUN_ID
```

Run records include the project name, attempt name and ID, Git state, file hashes, container image, logs, outcomes, and artifacts. Failed runs retain available evidence. The experiment host needs Slurm and Apptainer/Singularity or Docker; its storage must be accessible on compute nodes. Selecting `experiments = "local"` requires these tools locally.

| Variable | Coding shell | Dev command | Full experiment |
| --- | --- | --- | --- |
| `CALIGO_WORKSPACE` | Attempt workspace | Dev copy | Frozen execution copy |
| `CALIGO_EXECUTABLE` | Executable folder | Executable folder | Executable folder |
| `CALIGO_OUTPUTS` | Attempt outputs | Dev outputs | `/outputs` |
| `CALIGO_FIXED` | Unset | Unset | `/fixed` |

Bucket inputs and outputs use `rclone` (`s3://`, `gs://`, or configured remotes). Inputs are copied, never written back to their source. `caligo publish RUN_ID` retries bucket publication. Each artifact download gets its own run directory and refuses to overwrite an existing destination.

## Verification

```sh
pytest -q
CALIGO_TEST_DOCKER=1 pytest -q  # Also verifies experiment container behavior
```

Behavior checks cover project-scoped names, duplicate/unknown attempts, a real macOS terminal creating and switching attempts, persistent edits, filesystem isolation, independent Git metadata, dev output and exit status, frozen experiment inputs, separate host routing, failure evidence, and bucket storage. See [docs/verification.md](docs/verification.md) for evidence and limits.
