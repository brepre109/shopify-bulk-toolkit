from shopify_bulk.throttle import CostThrottle


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(avail=100.0, maximum=1000.0, rate=50.0):
    clock, slept = Clock(), []

    def sleep(s):
        slept.append(s)
        clock.t += s

    th = CostThrottle(maximum=maximum, available=avail, restore_rate=rate, clock=clock, sleep=sleep)
    return th, clock, slept


def test_no_wait_when_bucket_has_room():
    th, _, slept = make(avail=500)
    assert th.acquire(100) == 0
    assert slept == []
    assert th.available == 400


def test_waits_exactly_long_enough_to_refill():
    th, _, slept = make(avail=100, rate=50)
    th.acquire(300)  # needs 200 more points at 50/s
    assert slept == [4.0]
    assert th.available == 0


def test_refills_with_elapsed_time_up_to_maximum():
    th, clock, _ = make(avail=0, maximum=1000, rate=50)
    th.acquire(0)
    clock.t += 100  # would refill 5000, capped at 1000
    assert th.wait_time(1000) == 0
    assert th.available == 1000


def test_update_syncs_with_server_status():
    th, _, _ = make()
    th.update({"maximumAvailable": 2000.0, "currentlyAvailable": 150, "restoreRate": 100.0})
    assert (th.maximum, th.available, th.restore_rate) == (2000.0, 150.0, 100.0)
    assert th.wait_time(350) == 2.0


def test_cost_above_bucket_size_waits_for_full_bucket_only():
    th, _, slept = make(avail=0, maximum=1000, rate=50)
    th.acquire(5000)
    assert slept == [20.0]


def test_reserve_leaves_headroom_for_other_clients():
    th, _, _ = make(avail=300, rate=50)
    th.reserve = 250
    assert th.wait_time(100) == 1.0
