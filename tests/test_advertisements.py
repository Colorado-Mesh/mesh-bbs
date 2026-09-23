import json

import pytest

from mesh_bbs.advertisements import AdvertSchedule
from mesh_bbs.config import HostConfig, RadioConfig


def test_attempt_persists_before_send_and_is_due_after_24_hours(tmp_path):
    path = tmp_path / "advert.json"
    schedule = AdvertSchedule(path)
    assert schedule.delay(100000, 86400) == 0
    schedule.mark_attempt(100000)
    reopened = AdvertSchedule(path)
    assert reopened.delay(100001, 86400) == 86399
    assert reopened.delay(186400, 86400) == 0
    assert reopened.delay(10000, 86400) > 86400  # Clock rollback cannot cause a flood.


@pytest.mark.parametrize("value", [True, "yesterday", -1, float("nan"), float("inf")])
def test_invalid_state_does_not_allow_an_immediate_advert(tmp_path, value):
    path = tmp_path / "advert.json"
    path.write_text(json.dumps({"last_attempt": value}))
    with pytest.raises(ValueError):
        AdvertSchedule(path)


@pytest.mark.parametrize("interval", [-1, True, 1, 3599, 604801, 86400.0])
def test_config_rejects_unsafe_advert_intervals(interval):
    with pytest.raises(ValueError):
        RadioConfig(advert_interval_seconds=interval)


def test_adverts_are_opt_in_and_meshcore_only(tmp_path):
    assert RadioConfig().advert_interval_seconds == 0
    with pytest.raises(ValueError, match="only supported for MeshCore"):
        HostConfig("BBS", "test", tmp_path, meshtastic=RadioConfig(advert_interval_seconds=86400))
