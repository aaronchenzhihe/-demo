DARK_MV = 360
BRIGHT_MV = 180
SENSITIVITY = 5
PWM_CYCLE_MS = 20
SAMPLE_MS = 100
INITIAL_BRIGHTNESS = 0.0
LED_ACTIVE_HIGH = False
DISTANCE_LIMIT_CM = 100.0
DISTANCE_SAMPLE_MS = 200
DISTANCE_FAILURE_LIMIT = 3


class UltrasonicSensor:
    def __init__(self):
        from machine import Pin
        import utime

        self.trigger = Pin(Pin.GPIO13, Pin.OUT, Pin.PULL_DISABLE, 0)
        self.echo = Pin(Pin.GPIO12, Pin.IN, Pin.PULL_DISABLE, 0)
        self.clock = utime

    def read_cm(self):
        if self.echo.read() != 0:
            return None
        self.trigger.write(0)
        self.clock.sleep_us(2)
        self.trigger.write(1)
        self.clock.sleep_us(10)
        self.trigger.write(0)
        wait_start = self.clock.ticks_us()
        while self.echo.read() == 0:
            if self.clock.ticks_diff(self.clock.ticks_us(), wait_start) >= 30000:
                return None
        pulse_start = self.clock.ticks_us()
        while self.echo.read() == 1:
            if self.clock.ticks_diff(self.clock.ticks_us(), pulse_start) >= 50000:
                return None
        duration_us = self.clock.ticks_diff(self.clock.ticks_us(), pulse_start)
        distance_cm = duration_us / 58.0
        if 2 <= distance_cm <= 800:
            return round(distance_cm, 2)
        return None


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


def run():
    from misc import ADC, PWM
    import utime

    control = LightController(
        DARK_MV, BRIGHT_MV, SENSITIVITY, INITIAL_BRIGHTNESS
    )
    control.reset()
    gate = DistanceGate()
    if not isinstance(PWM_CYCLE_MS, int) or PWM_CYCLE_MS <= 0:
        raise ValueError("PWM cycle must be a positive integer in milliseconds")
    adc = ADC()
    initial_pwm = control.duty if LED_ACTIVE_HIGH else 100 - control.duty
    initial_high_time = int(initial_pwm * PWM_CYCLE_MS / 100.0 + 0.5)
    pwm = PWM(PWM.PWM2, PWM.ABOVE_MS, initial_high_time, PWM_CYCLE_MS)
    try:
        if adc.open() != 0:
            raise RuntimeError("ADC initialization failed")
        if pwm.open() != 0:
            raise RuntimeError("PWM initialization failed")
        sensor = UltrasonicSensor()
        previous_tick = utime.ticks_ms()
        log_tick = previous_tick
        distance_tick = None
        distance_cm = None
        applied_high_time = initial_high_time
        while True:
            utime.sleep_ms(SAMPLE_MS)
            now = utime.ticks_ms()
            if distance_tick is None or utime.ticks_diff(now, distance_tick) >= DISTANCE_SAMPLE_MS:
                try:
                    distance_cm = sensor.read_cm()
                except Exception:
                    distance_cm = None
                gate.update(distance_cm)
                distance_tick = utime.ticks_ms()
            now = utime.ticks_ms()
            elapsed_ms = utime.ticks_diff(now, previous_tick)
            previous_tick = now
            if elapsed_ms <= 0:
                continue
            dt_seconds = min(elapsed_ms / 1000.0, SAMPLE_MS * 2 / 1000.0)
            try:
                voltage_mv = adc.read(ADC.ADC0)
                target = control.target_from_mv(voltage_mv)
            except Exception:
                voltage_mv = None
                target = None
            if not gate.enabled:
                control.reset()
                brightness = 0.0
                target = 0.0
            elif voltage_mv is not None:
                brightness = control.update(voltage_mv, dt_seconds)
            else:
                brightness = control.duty
            pwm_duty = brightness if LED_ACTIVE_HIGH else 100.0 - brightness
            high_time = int(pwm_duty * PWM_CYCLE_MS / 100.0 + 0.5)
            if high_time != applied_high_time:
                if pwm.open(PWM.ABOVE_MS, high_time, PWM_CYCLE_MS) != 0:
                    raise RuntimeError("PWM update failed")
                applied_high_time = high_time
            if utime.ticks_diff(now, log_tick) >= 1000:
                target_text = "--" if target is None else "%.1f" % target
                voltage_text = "--" if voltage_mv is None else voltage_mv
                distance_text = "--" if distance_cm is None else distance_cm
                print("ADC0=%smV target=%s%% brightness=%.1f%% PWM=%.1f%% high=%sms distance=%scm enabled=%s" % (
                    voltage_text, target_text, brightness,
                    applied_high_time * 100.0 / PWM_CYCLE_MS, applied_high_time,
                    distance_text, gate.enabled
                ))
                log_tick = now
    finally:
        try:
            pwm.close()
        finally:
            adc.close()


if __name__ == "__main__":
    run()