# Benchmark scenarios

Drop grid2op environment directories here, one directory per scenario. A
request selects one by its directory name in `benchmark.scenarios[].env_name`.
A name with no directory here is treated as a grid2op built-in environment,
such as `l2rpn_case14_sandbox`.

This directory is bind-mounted into the container at `/app/scenarios`, so a new
scenario needs no image rebuild.
