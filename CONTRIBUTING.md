# Contributing

This is a working bench, a recorded dataset and a classifier that runs on the
microcontroller taking the measurements. Contributions are welcome, and the most
useful ones are listed at the end.

## Before you open a pull request

Run the checks. They are fast, they need no hardware, and they are the same
ones used while the work was done.

```bash
python tools/e2e_check.py
python tools/check_calibration.py
python tools/validate_rules.py
python tools/evaluate_model.py
python tools/plot_dataset.py
cd firmware && make
```

What each one is for:

- `python tools/e2e_check.py`: 115 assertions over the wire format, the validator, session writing, consolidation, the live cards, board identity and a SHA-256 manifest of the four acquisition-critical files
- `python tools/check_calibration.py`: every sensor constant has exactly one definition, and no front end has grown a private copy
- `python tools/validate_rules.py`: replays the firmware rule ladder over every valid run; false alarms on healthy windows must stay at zero
- `python tools/evaluate_model.py`: retrains both model variants and rewrites the figures and model/evaluation.json
- `python tools/plot_dataset.py`: rebuilds every dataset figure and table from the recorded runs
- `make`: build/motor.bin must come out byte-identical to the committed motor.bin

A change that makes a check fail needs the check updated in the same commit,
with the reason in the message. Do not skip or delete a check to make it pass.

## House rules

- Numbers in documentation must be reproducible by a command in this repository.
  If you cannot generate it, do not write it.
- No personal names, instrument serial numbers, MAC or IP addresses, or absolute
  paths in committed files.
- Keep any value defined in exactly one place. Several checks here exist only
  because a constant was once written down twice and drifted.

## Useful things to work on

- More `overheat` runs. The class has one run, so its cross-run behaviour is unproven.
- More `loose_mount` runs at more speeds. No single channel separates it today.
- A regenerable on-board tree. `firmware/fault_tree.c` cannot be rebuilt from this repository.
- Porting the acquisition backend to another board. The pin map is the only board-specific part.

## Licence

Contributions are accepted under the MIT licence in `LICENSE`.
