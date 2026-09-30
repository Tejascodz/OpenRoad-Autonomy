from app.services.battery_model import BatteryModel


def test_consumption_grows_with_speed_and_grade():
    b = BatteryModel(2.5)
    flat_slow = b.calculate_consumption(1000, 10)
    assert b.calculate_consumption(1000, 25) > flat_slow
    assert b.calculate_consumption(1000, 10, 5) > flat_slow
    assert b.calculate_consumption(1000, 10, -5) < flat_slow


def test_consume_and_reserve():
    b = BatteryModel(2.5)
    r = b.consume_energy(1000, 15)
    assert r["success"] and 0 < b.get_percentage() < 100
    assert b.usable_kwh() < b.stats.current_charge_kwh  # 10 % reserve is not usable
    assert not b.can_complete_energy(10.0)
    assert b.can_complete_energy(0.1)


def test_insufficient_energy_is_refused():
    b = BatteryModel(0.01)
    assert not b.consume_energy(100_000, 15)["success"]


def test_charge_is_capped_at_capacity():
    b = BatteryModel(2.5)
    b.consume_energy(5000, 15)
    before = b.get_percentage()
    b.charge(0.1)
    assert b.get_percentage() > before
    b.charge(100)
    assert b.get_percentage() == 100.0
