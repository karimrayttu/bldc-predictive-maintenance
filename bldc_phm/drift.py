"""Online drift detectors: EWMA + CUSUM + 3-sigma with N-frame persistence."""


class EWMA:
    def __init__(self, alpha=0.05):
        self.alpha = alpha
        self.value = None

    def update(self, x):
        self.value = x if self.value is None else self.alpha * x + (1 - self.alpha) * self.value
        return self.value


class CUSUM:
    """Two-sided cumulative sum on standardized residuals (in sigma units)."""
    def __init__(self, k=0.5):
        self.k = k          # slack; ignores drift smaller than k*sigma
        self.hi = 0.0
        self.lo = 0.0

    def update(self, z):    # z = (x - mu) / sigma
        self.hi = max(0.0, self.hi + z - self.k)
        self.lo = min(0.0, self.lo + z + self.k)
        return self.hi, self.lo


class SeverityTracker:
    """
    Fuses a 3-sigma test (with persistence) and CUSUM into an S0..S3 label for one
    feature (e.g. ripple_permil). Call set_baseline() once, then update() per frame.
    """
    S_LABELS = {0: "S0 Healthy", 1: "S1 Incipient", 2: "S2 Developing", 3: "S3 Severe"}

    def __init__(self, k_warn=3.0, k_dev=4.0, k_sev=6.0, persistence=10,
                 cusum_k=0.5, cusum_warn=5.0, ewma_alpha=0.05):
        self.k_warn, self.k_dev, self.k_sev = k_warn, k_dev, k_sev
        self.persistence = persistence
        self.cusum_warn = cusum_warn
        self.ewma = EWMA(ewma_alpha)
        self.cusum = CUSUM(cusum_k)
        self.mu = None
        self.sigma = None
        self._over = 0
        self.severity = 0

    def set_baseline(self, mu, sigma):
        self.mu = float(mu)
        self.sigma = max(float(sigma), 1e-9)   # avoid divide-by-zero on flat channels
        self.ewma.value = mu
        self.cusum.hi = self.cusum.lo = 0.0
        self._over = 0
        self.severity = 0

    def update(self, x):
        """Return (severity:int, info:dict). Safe to call before baseline is set."""
        if self.mu is None:
            return 0, {"z": 0.0, "ewma": x, "cusum_hi": 0.0}
        z = (x - self.mu) / self.sigma
        ew = self.ewma.update(x)
        hi, lo = self.cusum.update(z)

        az = abs(z)
        if az >= self.k_warn:
            self._over += 1
        else:
            self._over = max(0, self._over - 1)   # hysteresis: decay, don't snap to 0

        sev = 0
        # CUSUM catches slow drift -> incipient even before 3-sigma trips
        if max(hi, -lo) >= self.cusum_warn:
            sev = max(sev, 1)
        if self._over >= self.persistence:
            if az >= self.k_sev:
                sev = 3
            elif az >= self.k_dev:
                sev = max(sev, 2)
            else:
                sev = max(sev, 2)
        self.severity = sev
        return sev, {"z": z, "ewma": ew, "cusum_hi": hi, "cusum_lo": lo, "over": self._over}
