# lumi-agent-sandbox

`lumi-agent-sandbox` is a small host-side harness for running an AI coding agent on LUMI inside a disposable task workspace.

It does not try to replace the LAIF agent container. It wraps it with stricter defaults:

- no `$HOME` mount by default
- one workspace per task
- read-only `/input`
- writable `/workspace`, `/output`, `/jobs`, and `/logs`
- host-side Slurm validation before `sbatch`
- manual diff/archive/destroy flow

## Basic Use

```sh
lumi-agent-sandbox create my-task
lumi-agent-sandbox enter my-task
lumi-agent-sandbox submit my-task jobs/test.sh
lumi-agent-sandbox diff my-task
lumi-agent-sandbox archive my-task
lumi-agent-sandbox destroy my-task --yes
```

By default, sandboxes are created under:

```text
/scratch/project_462000131/$USER/agent-sandboxes
```

Override that with:

```sh
lumi-agent-sandbox --account project_other create my-task
lumi-agent-sandbox --root /scratch/project_other/$USER/agent-sandboxes create my-task
```

## Slurm Policy

Each sandbox has a `policy.yaml`. The generated default allows small, short jobs only:

- max time: `00:30:00`
- max nodes: `1`
- max GPUs per node: `1`
- allowed partitions: `small`, `standard`, `dev-g`, `small-g`, `standard-g`

`submit` accepts only scripts inside the sandbox `jobs/` directory. It rejects obvious references to home directories, broad project/scratch paths outside the sandbox, excessive resources, and log paths outside `logs/`.

The generated container wrappers shadow `sbatch`, `srun`, and `salloc` inside the agent container. Submit jobs from the host:

```sh
lumi-agent-sandbox submit my-task jobs/test.sh
```

## Install For Development

```sh
python3 -m pip install -e .
```

The repository includes a small `setup.py` so editable installs work with older `pip` versions commonly found on HPC systems.

Run tests with the standard library:

```sh
python3 -m unittest discover -s tests
```
