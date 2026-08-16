"""
Taxonomy: the editable source of truth for every test variable.

`fields` declares each variable (type + options + group + default); the form and the
uniform per-run meta.json record are generated from it. `test_templates` are presets that
pre-fill the form. New dropdown values typed in the app persist back to taxonomy.yaml.
"""

import os
import yaml
from collections import defaultdict


class Taxonomy:
    def __init__(self, path):
        self.path = path
        self.data = {}
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                self.data = yaml.safe_load(f) or {}
        else:
            self.data = {}

    def save(self):
        with open(self.path + ".tmp", "w", encoding="utf-8") as f:
            yaml.safe_dump(self.data, f, sort_keys=False, allow_unicode=True, width=100)
        os.replace(self.path + ".tmp", self.path)

    # accessors
    def list(self, key):
        v = self.data.get(key, [])
        return list(v) if isinstance(v, list) else []

    def fields(self):
        return self.data.get("fields", [])

    def field_keys(self):
        """Ordered metadata keys (+ the auto 'label') written uniformly every run."""
        keys = [f["key"] for f in self.fields()]
        return keys + (["label"] if "label" not in keys else [])

    def field_groups(self):
        declared = self.data.get("field_groups")
        if declared:
            return declared
        seen = []
        for f in self.fields():
            if f.get("group", "General") not in seen:
                seen.append(f.get("group", "General"))
        return seen

    def fields_in_group(self, group):
        return [f for f in self.fields() if f.get("group", "General") == group]

    def test_templates(self):
        return self.data.get("test_templates", {})

    def essential_fields(self):
        """Fields for the main form, in the declared essentials order; rest are advanced."""
        order = self.data.get("essentials", [])
        by_key = {f["key"]: f for f in self.fields()}
        ess = [by_key[k] for k in order if k in by_key]
        ess_keys = set(order)
        adv = [f for f in self.fields() if f["key"] not in ess_keys]
        return ess, adv

    def speed_steps(self):
        return self.data.get("speed_steps", [])

    def rpm_for_step(self, step):
        for s in self.speed_steps():
            if int(s.get("step", -1)) == int(step):
                return s.get("rpm", "")
        return ""

    def matrix_axes(self):
        m = self.data.get("matrix", {})
        return m.get("rows", "speed_rpm_nominal"), m.get("cols", "load")

    # mutation (user typed a new value)
    def add_value(self, key, value):
        value = (value or "").strip()
        if not value:
            return False
        lst = self.data.get(key)
        if not isinstance(lst, list):
            return False
        if value not in lst:
            lst.append(value)
            self.save()
            return True
        return False

    # label generation
    def make_label(self, fields):
        """Build a clean, sortable run label:  condition[-severity]_orientation_load_NNNNrpm.
        rpm is zero-padded to 4 digits so labels sort in speed order; severity is appended
        only for faults (skipped when none/healthy)."""
        template = self.data.get("label_template",
                                 "{condition}{sev_suffix}_{orientation}_{load}_{rpm}")
        safe = defaultdict(str, {k: str(v).strip().replace(" ", "-")
                                 for k, v in fields.items() if v not in (None, "")})
        # derived tokens
        sev = safe.get("severity", "")
        safe["sev_suffix"] = "" if sev in ("", "none") else f"-{sev}"
        rpm_raw = str(fields.get("rpm", "")).strip()
        try:
            safe["rpm"] = f"{int(float(rpm_raw)):04d}rpm"
        except (TypeError, ValueError):
            safe["rpm"] = f"{rpm_raw}rpm" if rpm_raw else ""
        try:
            label = template.format_map(safe)
        except Exception:
            label = "_".join(p for p in (safe.get("condition", "run"), safe.get("orientation"),
                                         safe.get("load"), safe.get("rpm")) if p)
        while "__" in label:
            label = label.replace("__", "_")
        return label.strip("_")
