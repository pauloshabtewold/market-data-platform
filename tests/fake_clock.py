class FakeClock:
    """A monotonic clock and sleep that advance only when told to."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
