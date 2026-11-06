from usr.config import DISTANCE_FAILURE_LIMIT, DISTANCE_LIMIT_CM


class DistanceGate:
    def __init__(self):
        self.enabled = False
        self.failures = 0

    def update(self, distance_cm):
        if distance_cm is None or not 2 <= distance_cm <= 800:
            self.failures += 1
            if self.failures >= DISTANCE_FAILURE_LIMIT:
                self.enabled = False
        else:
            self.failures = 0
            self.enabled = distance_cm <= DISTANCE_LIMIT_CM
        return self.enabled