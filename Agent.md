# Caligo: functional requirements

Build a minimal setup and execution shell for humans and agents. The product and command are named `caligo`. This document specifies the agreed design: projects containing attempts, isolated coding shells, remote development commands through `caligo exec`, and full experiments through `caligo run`. README.md documents the current CLI; docs/verification.md records the checks and their limits.

## Projects, attempts, and runs

- A project, such as `dropout-study`, owns its manifest, declared inputs, fixed evaluator, and execution settings. The project directory selects the project, like a repository directory selects a Git repository.
- An attempt, such as `baseline` or `higher-dropout`, is a separate persistent coding workspace inside that project. A project may contain many attempts.
- A run is a frozen experiment submission from one attempt. An attempt may produce many runs.
- `caligo create ATTEMPT --mode supervisor|user` creates a named attempt in the current project and enters its coding shell. It reads the project's existing `project.toml`; the positional argument names the attempt, not the manifest or project. Duplicate attempt names fail rather than overwrite an existing workspace.
- `caligo list` lists attempts within the current project and marks the current attempt.
- `caligo switch ATTEMPT` preserves the current attempt and enters an existing attempt's coding shell. The outer controller performs the transition and applies the target attempt's access rules; it does not expose neighboring workspaces to the original shell.
- To change projects, leave the coding shell and change to the other project's directory. Attempt switching does not select a different project.

User interface:

```sh
cd dropout-study
caligo create baseline --mode user
caligo list
caligo switch higher-dropout  # An existing attempt in dropout-study
caligo exec -- python check.py
caligo run
exit
```

## Projects

- Create a project by preparing `project.toml`, its experimentation filesystem, and fixed setup. Support manual creation and setup tools.
- Keep hostnames, accounts, partitions, storage paths, and site-specific setup configurable; generated projects must be portable to other users and clusters.
- Declare compute, image, editable folders, readable folders, an executable workspace, and an outputs destination. Optionally declare a default experiment script.
- Keep fixed setup outside coding workspaces and their Git history. Only the full experiment runner supplies fixed setup, mounted read-only when needed.

Illustrative manifest:

```toml
name = "dropout-study"
cpu = 8
gpu = "h200:4"
image = "my-caligo-image:latest"
editable = ["src", "configs"]
readable = ["data", "reference"]
executable = "executable"
eval = "eval.sh"
outputs = "s3://caligo-results/dropout-study/"

[locations]
coding = "local"
dev = "dev-host"
experiments = "cluster"
```

## Coding attempts

Require three explicit locations in every manifest: `locations.coding`, `locations.dev`, and `locations.experiments`. Do not infer or default any location. Coding identifies the host for the isolated shell and persistent working files; dev identifies the remote development host; experiments identifies the full-experiment host. A location uses an SSH host alias, or `local` where local execution is selected explicitly.

`caligo create ATTEMPT --mode supervisor|user` reads the prepared project definition outside the attempt workspace, creates an isolated coding workspace with persistent working files, and immediately opens a fresh shell inside it. Start the shell in the executable directory. Creation does not submit a dev command or allocate an experiment job.

The attempt workspace must contain only explicitly declared readable, editable, and executable content. Assemble it from the declarations; undeclared project files must not exist there. Deny access to the original project directory, fixed setup, harness records, other attempts, and original repository. Standard shell tools and installed dependencies remain available outside the workspace.

The purpose is to prevent accidental access to or damage to neighboring attempts, not to defend against a hostile user. Reuse an existing OS sandbox runtime rather than building isolation from scratch. Use Sandbox Runtime (`srt`) as the initial integration, backed by macOS sandbox-exec or Linux Bubblewrap; implement and verify macOS first. macOS access restrictions do not create a separate filesystem root. A fixed `/project` mount path is not required. Keep CPU, memory, and disk overhead small by reusing installed tools, avoiding a VM or image per attempt, and running expensive checks remotely.

Any Git history available inside must contain only declared content. Keep Git control metadata outside the attempt's project files and separate from other attempts' metadata, so Git operations cannot modify neighboring workspaces or expose the original repository. Merely switching worktrees is not an isolation boundary.

| Area | Supervisor | User |
| --- | --- | --- |
| Editable folders | Read/write | Read/write |
| Readable folders | Read/write | Read-only |
| Executable workspace | Read/write and launch programs | Read/write and launch programs |
| Fixed setup | Not exposed | Not exposed |

- Each attempt has its own persistent working files. Exiting the shell preserves them; `caligo switch ATTEMPT` reopens an existing attempt. Creating another attempt creates a separate workspace and enters its shell.
- Commits and pushes remain explicit. Concurrent environments must not overwrite one another's working files.
- Ordinary shell commands and coding agents run inside the attempt's isolated coding shell. A plain `python check.py` runs there; only `caligo exec` routes a command to dev, and only `caligo run` submits a full experiment.
- Make `caligo exec`, `caligo run`, and run inspection available inside the coding shell, automatically associated with its environment and current files. Provide the control interface without exposing controller storage or a host container-management socket.
- Allow subfolders inside writable areas. Do not let a caligo environment add new exposed root folders.
- Launch the coding shell, dev commands, and experiments with the executable workspace as their working directory. This is a launch-directory rule, not a restriction on where interpreters may read declared code.
- Keep coding environments independent of experiment GPU allocations, so idle coding environments do not reserve experiment GPUs.

## Remote development

`caligo exec -- COMMAND` runs a command on the explicitly configured remote dev host:

1. Snapshot the coding environment's current declared files, including uncommitted edits.
2. Transfer that snapshot into a separate dev working directory, preserving the declared access modes and excluding fixed setup and undeclared project files.
3. Run the supplied command from its executable directory using the dev host's environment.
4. Stream stdout and stderr back to the coding terminal and return the command's exit code.

Preserve dev outputs separately. Do not silently synchronize remote edits back over coding files. Subsequent coding edits do not change an already dispatched dev command's inputs. Dev commands do not submit Slurm jobs.

```sh
caligo create baseline --mode user     # Create an attempt and enter its shell
# Edit code here, yourself or with a coding agent.
caligo exec -- python check.py          # Execute remotely on dev
caligo run                             # Submit a full experiment
exit                                     # Leave; coding files persist
```

## Experiment execution

- All full experiment executions go through `caligo run`, which snapshots the current coding files and submits prepared work through a job scheduler. Remote development checks use `caligo exec`.
- Support both a default script declared by `execute` in `project.toml` and an explicit script supplied to `caligo run`. An explicit script overrides the default; both must be inside the executable workspace. If neither is provided, require the user to select a script.
- Full experiments use Slurm at the explicitly configured experiment location. Native dev commands do not submit Slurm jobs.
- Run experiments in execution sandboxes with the requested resources and environment, supplying fixed setup separately from the interactive workspace.
- Record the inputs actually submitted, including uncommitted changes when supported. Subsequent edits must not change an already submitted run.
- Preserve the submitting worktree identity, Git revision, and dirty contents with each run.
- Give each run an automatic ID and expose its status, logs, outcome, and artifacts. Preserve available evidence for failed runs.
- Keep experiment strategy with users or agents. Use Git for collaboration rather than requiring a separate experiment registry or manually named experiment objects.

```sh
caligo run                          # Use the project's default script
caligo run executable/train.py      # Select a script explicitly
caligo run executable/benchmark.py
```

## Storage

- Support filesystem and bucket inputs and outputs.
- Store each run's records and artifacts under its own location beneath `outputs`.
- Keep working files and results separate. Supervisor edits must not silently change another environment's files or a shared input bucket.


## Verification

Keep tests focused on high-level behavior so implementation details can evolve. Verify attempt entry as a human using a real shell; a live agent test is not required. Check that `caligo create` enters the isolated coding shell, the workspace contains only declared content, excluded files cannot be accessed directly or through Git history, and exiting/switching preserves edits. Verify project-scoped attempt listing and switching, protection of neighboring attempts including their Git metadata, and run association with the submitting attempt. Verify that ordinary commands execute in coding, `caligo exec` uses a frozen copy on dev and returns output/exit status without overwriting coding files, and `caligo run` submits only to the experiment host.
