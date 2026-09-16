from misc import PWM_V2
import utime


# SG90: 在两个位置之间循环摆动（按你的机械结构可再微调占空比）
pwm = PWM_V2(PWM_V2.PWM2, 100.0, 15)


def servo_loop():
    while True:
        print("关灯")
        pwm.open(100.0, 10)
        utime.sleep(1)

        print("开灯")
        pwm.open(100.0, 20)
        utime.sleep(1)


if __name__ == "__main__":
    servo_loop()