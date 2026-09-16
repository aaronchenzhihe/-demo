def foo(s):
    n = int(s)
    print('>>> n = %d' % n)
    return 10 / n

def main():
    x=foo(10)
    print('>>> x = %d' % x)

main()