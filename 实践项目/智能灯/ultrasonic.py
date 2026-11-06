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