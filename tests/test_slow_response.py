from unittest import TestCase
import requests
import time


class TestSlowResponse(TestCase):
    def test_loop_requests(self):
        expected_success = 1000
        success_cnt = 0
        for i in range(expected_success):
            try:
                r = requests.get('http://localhost:9101/metrics', timeout=0.1)
                print('r', r)
                if r.status_code == 200:
                    success_cnt += 1
                time.sleep(0.1)
            except Exception as err:
                print('err', err)
                pass
        self.assertEqual(expected_success, success_cnt)
