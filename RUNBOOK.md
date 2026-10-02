# Runbook

What to type. See `README.md` for what any of it means.

## Once

```sh
ssh anisrahm@lumi.csc.fi
module load cray-python
cd /scratch/project_462000131/$USER
git clone https://github.com/aniskhan25/lumi-agent-sandbox.git
cd lumi-agent-sandbox
python3 -c "import yaml" || python3 -m pip install --user PyYAML
```

Put `module load cray-python` in `~/.bashrc`. LUMI's default `python3` is 3.6 and too old, and the
broker is forked as a child of `enter` — started under the wrong Python it fails silently, with the
only trace in `audit/broker.log`.

Everything below uses `python3 -m lumi_agent_sandbox`, which needs nothing installed and no PATH
entry. `pip install --user -e .` also works under `cray-python` if you want the shorter command.

## Every session

```sh
module load cray-python
cd /scratch/project_462000131/$USER/lumi-agent-sandbox
git pull
```

## Check the sandbox holds

```sh
python3 -m lumi_agent_sandbox create smoke
python3 -m lumi_agent_sandbox inspect smoke     # read the "Not enforced" section
python3 -m lumi_agent_sandbox verify smoke      # container probes run for real here
python3 -m lumi_agent_sandbox destroy smoke --yes
```

## Run a task

```sh
python3 -m lumi_agent_sandbox create fix-dataloader
SANDBOX=/scratch/project_462000131/$USER/agent-sandboxes/fix-dataloader

cp -r examples/forest-patches/* "$SANDBOX/work/"
python3 "$SANDBOX/work/make_input.py" "$SANDBOX/input"

python3 -m lumi_agent_sandbox enter fix-dataloader
```

`make_input.py` runs on the host because `/input` is read-only inside, which is the point.

`enter` queues a CPU allocation, starts the broker on the login node, and opens OpenCode on a compute
node. Expect to wait for the allocation. The prompt to paste is in
`examples/forest-patches/README.md`.

Afterwards:

```sh
cat "$SANDBOX"/logs/*.out
cat "$SANDBOX"/output/patch_report.json
python3 -m lumi_agent_sandbox destroy fix-dataloader --yes   # audit trail is kept
```

## Inside the container

The agent has these and nothing else:

```text
lumi-job submit jobs/<script>.sh [--partition dev-g] [--time 00:10:00] [--nodes 1] [--gpus 1]
lumi-job status <job-id>
lumi-job cancel <job-id>
```

`/workspace` (code, rw) · `/input` (ro) · `/output` · `/jobs` · `/logs`. No `sbatch`, no `$HOME`,
no credentials.

## Roihu, via FirecREST

```sh
ssh anisrahm@roihu-gpu.csc.fi
export FIRECREST_TOKEN=$(cat ~/.firecrest-token)
python3 -m lumi_agent_sandbox --site-config site-roihu.yaml create smoke
python3 -m lumi_agent_sandbox --site-config site-roihu.yaml submit smoke jobs/hostname.sh --dry-run
```

Personal tokens come from https://my.csc.fi/firecrest-token and last 24 hours. Store with
`umask 077; printf %s 'eyJ...' > ~/.firecrest-token`. Never put one in a site config — those are
committed. There is no agent on Roihu yet: no OpenCode image exists there, so `site-roihu.yaml`
stays at `job_execution: host` and only the submit path works.

## When it goes wrong

| Symptom | Cause |
|---|---|
| `future feature annotations is not defined` | LUMI's default Python 3.6. `module load cray-python`. |
| `command not found: lumi-agent-sandbox` | `~/.local/bin` not on PATH. Use `python3 -m lumi_agent_sandbox`. |
| `requires a different Python` | Needs 3.9+. `module load cray-python` (LUMI) or `python-data` (Roihu). |
| `Directory cannot be installed in editable mode` | pip too old for a `pyproject.toml`-only project. Skip the install and use `python3 -m`. |
| Agent asks permission for every command | Pre-`3d6db7b` sandbox. `git pull` and re-create it. |
| `session budget exceeded` before any job | The agent's own allocation is charged too. Raise `max_node_hours_per_session`. |
| `CERTIFICATE_VERIFY_FAILED` | Python without a CA bundle. `pip install --user certifi`, `export SSL_CERT_FILE=$(python3 -m certifi)`. |
| `FIRECREST_TOKEN may have expired` | 24h personal token. Get a new one. |
| Agent hunts for numpy | Pre-`3d6db7b` sample. `git pull`; it is stdlib-only now. |

## Reading a session afterwards

```sh
cat "$SANDBOX/audit/broker.log"        # every request, accepted or rejected
cat "$SANDBOX/audit/jobs.jsonl"        # what was submitted, and the budget spend
cat "$SANDBOX/audit/manifest.json"     # image hash, effective limits, mounts, profile
tail -50 "$SANDBOX"/state/home/.local/share/opencode/log/opencode.log
```

Empty `jobs/` and `logs/` means the agent never submitted anything.
