# HAI session scenarios

Put grid2op environment directories here, one directory per environment. A
`RunHAISession` request selects an environment by directory name in
`scenario.env_name`, and one chronic folder inside it in `scenario.scenario_name`.

This directory is bind-mounted read-only into two containers:

| Container | Mount point | Used for |
|---|---|---|
| `hai-session-runner` | `/app/scenarios` | Validation, and the agent wrapper's copy of the environment |
| `PowerGrid-simulator-app` | `/code/Ressources/hai_scenarios` | The episode the participant plays |

A new scenario needs no rebuild or restart. It takes effect the next time a
participant clicks "Load simulation".

## Required format

The PowerGrid simulator builds its environment with per-series CSV handlers,
so an environment here must look like the simulator's own
`Ressources/env_ICAPS_input_data_test`:

```
<env_name>/
  config.py, grid.json, prods_charac.csv, ...   (a normal grid2op environment)
  chronics/
    <scenario_name>/
      prod_p.csv.bz2              load_p.csv.bz2         prod_v.csv.bz2
      load_q.csv.bz2              prod_p_forecasted.csv.bz2
      load_p_forecasted.csv.bz2   load_q_forecasted.csv.bz2
      ...                          (.csv works as well as .csv.bz2)
```

A request whose chronic folder lacks any of the seven series is rejected
before a session starts. Benchmarking scenarios usually lack the `_forecasted`
files, so they can't be reused as they are.

**Episode length** is whatever the chronic contains from
`scenario.scenario_first_step` on. The simulator has no max-steps setting
(spec section 6.3, IAI-5), so to get a shorter episode, prepare a shorter chronic.

To start with the simulator's own test environment:

```bash
cp -r ../InteractiveAI/usecases_examples/PowerGrid/Ressources/env_ICAPS_input_data_test .
```

Then request `{"env_name": "env_ICAPS_input_data_test", "scenario_name": "2035-01-01_0"}`.
