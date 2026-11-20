# FIFO test queue

A local FIFO queue shared by all your projects and worktrees. One development
check runs at a time. Requires macOS or Linux and Python 3.9+. No dependencies.

Useful when multiple terminals, projects or coding agents share a machine and
run resource-intensive checks. All participating commands use the same queue,
so development work can continue while checks wait their turn.

## Installation

Clone the repository and add its directory to your `PATH`:

```sh
git clone https://github.com/akamf/FIFO-test-queue.git "$HOME/.local/share/test-queue"
export PATH="$HOME/.local/share/test-queue:$PATH"
```

Add the `export` line to your shell configuration to keep the command available
in new terminals. Alternatively, invoke the executable by its absolute path.

## Usage

Run commands from the project directory you want to check:

```sh
test-queue test
test-queue test -- path/to/example.test.ts
test-queue run -- npm run lint
test-queue run -- npm run typecheck
test-queue run -- python3 -m unittest discover -s tests -v
test-queue status
test-queue history
```

`test` is a convenience command for npm projects using Vitest: it runs
`npm test -- … --maxWorkers=2`. Use `run` for other test runners, builds or checks;
it executes exactly the supplied command. Both preserve the working directory,
environment, terminal output and exit status. Quote shell expressions explicitly,
e.g. `test-queue run -- sh -c 'cmd1 && cmd2'`.
Do not nest queued commands: the inner command would wait for its own parent.

Only commands started through this tool participate. Existing jobs, direct npm
commands and other agents are unaffected until their commands are routed here.
CI needs no changes. This does not cache results or combine different branches.

State is shared at `~/.local/state/test-queue`; `TEST_QUEUE_STATE_DIR` overrides it.
Keep that setting identical across participating shells. Job metadata and history
contain command arguments and working directories: do not pass secrets as arguments.
History is append-only; `history` displays the last 20 records.

Ctrl-C, TERM and HUP are forwarded to the command's process group. The queue waits
for the command to exit before releasing the slot. Kernel file locks recover from
crashed waiters automatically. The command inherits its ticket lock, so killing the
queue wrapper does not let another job overlap the still-running command. Do not
launch background/detached work through the queue; the slot tracks the foreground
command, not independently detached descendants. Interactive job-control commands
are outside its scope.

## Shared workflows

Route heavy checks from every participating terminal or coding agent through the
queue. For agent workflows, add this convention to the project's agent instructions
and include the executable's absolute path if it is not available on `PATH`.
Keep the shared state directory consistent across sessions and run queued commands
in the foreground. Coding and research can continue in parallel.

Use `history` to compare check durations, and monitor system responsiveness, CPU
use and swap activity to assess the effect on your machine. The queue does not
interrupt jobs that were already running.

## Verification

```sh
python3 -m unittest discover -s tests -v
```

Tests use isolated temporary queues and small commands, without running project tests.

## Resource controls

Commands run with `nice -n 10`, giving interactive work higher scheduling priority.
This is not a hard CPU cap. On macOS, the head job waits while the kernel reports
warning or critical memory pressure, probing every two seconds, for at most 30
seconds after reaching the front of the queue. It then starts one low-priority job
even if pressure remains elevated, so unrelated applications cannot stall the
queue indefinitely. `TEST_QUEUE_MEMORY_WAIT_SECONDS` adjusts this grace period;
`0` disables the memory pause without disabling serialization. Ctrl-C cancels a
waiting job. Existing jobs are never stopped. If telemetry is unavailable, a warning
is printed and the command still benefits from serialization. Linux has no memory
gate. Swap size alone does not block jobs.

New history records include wait/run seconds, CPU user/system seconds and
`peak_process_rss_bytes` from `wait4`. These are operating-system child accounting
statistics; peak RSS is a process high-water mark, not a simultaneous sum of the
whole process tree. CPU accounting includes descendants only insofar as they are
reaped/accounted by their parents. Commands killed with their wrapper may have no
completion record. Metrics are useful for comparison, not a complete machine trace.

Sandboxed tools may need permission to access the shared state directory. Do not
work around that by choosing a separate temporary queue: it would allow overlap.
