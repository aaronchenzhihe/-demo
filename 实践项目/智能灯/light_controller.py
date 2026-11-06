class LightController:
    def __init__(self, dark_mv, bright_mv, sensitivity, initial_duty=0.0):
        if dark_mv < 0 or bright_mv < 0 or dark_mv == bright_mv:
            raise ValueError("Invalid dark/bright calibration")
        if not 1 <= sensitivity <= 10:
            raise ValueError("Sensitivity must be between 1 and 10")
        if not 0 <= initial_duty <= 100:
            raise ValueError("Initial brightness must be between 0 and 100")
        self.dark_mv = dark_mv
        self.bright_mv = bright_mv
        response = (sensitivity - 1) / 9.0
        self.filter_seconds = 2.5 - 2.38 * response
        self.rate_per_second = 5.0 + 75.0 * response
        self.duty = float(initial_duty)
        self.filtered_target = self.duty

    def reset(self):
        self.duty = 0.0
        self.filtered_target = 0.0

    def target_from_mv(self, voltage_mv):
        if voltage_mv < 0:
            raise ValueError("ADC read failed")
        target = 100.0 * (self.bright_mv - voltage_mv)
        target /= self.bright_mv - self.dark_mv
        return max(0.0, min(100.0, target))

    def update(self, voltage_mv, dt_seconds):
        if dt_seconds <= 0:
            raise ValueError("Sample interval must be positive")
        target = self.target_from_mv(voltage_mv)
        alpha = dt_seconds / (self.filter_seconds + dt_seconds)
        self.filtered_target += alpha * (target - self.filtered_target)
        change = self.filtered_target - self.duty
        max_change = self.rate_per_second * dt_seconds
        self.duty += max(-max_change, min(max_change, change))
        self.duty = max(0.0, min(100.0, self.duty))
        return self.duty