from prometheus_client import start_http_server
from absl import logging, app
import time
from prometheus_client import Counter
from contextlib import redirect_stderr
import os

c = Counter('my_failures', 'Description of counter')


def loop(x):
    s = ''
    for i in range(x):
        s += str(i)
    return s

LOOP_SIZE = 10000000
# LOOP_SIZE = 10

def main(argv):
    for i in range(1000):
        c.inc()
        loop(LOOP_SIZE)
        time.sleep(1)
        print('loop', i)


if __name__ == '__main__':
    try:
        with redirect_stderr(open(os.devnull, "w")):
            start_http_server(9101)
    except:
        pass

    app.run(main)
