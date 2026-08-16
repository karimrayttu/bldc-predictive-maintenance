## What this changes

Describe the change and why it is needed.

## Checks

Paste the output. A pull request that changes code needs these to pass:

```
python tools/e2e_check.py
python tools/check_calibration.py
```

If the change touches the dataset, the model or the firmware rules, also run
`python tools/validate_rules.py` and `python tools/evaluate_model.py`, and update any
number in the documentation that moved as a result.

## Notes

- Numbers in documentation must be reproducible by a command in this repository.
- No personal names, instrument serials, MAC or IP addresses, or absolute paths.
