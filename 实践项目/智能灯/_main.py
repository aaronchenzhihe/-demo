from usr.config import (
    DARK_MV,
    BRIGHT_MV,
    SENSITIVITY,
    PWM_CYCLE_MS,
    SAMPLE_MS,
    INITIAL_BRIGHTNESS,
    LED_ACTIVE_HIGH,
    DISTANCE_SAMPLE_MS,
)
from usr.distance_gate import DistanceGate
from usr.light_controller import LightController
from usr.ultrasonic import UltrasonicSensor


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