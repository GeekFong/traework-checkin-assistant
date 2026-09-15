# -*- coding: utf-8 -*-
"""t8：history_calendar 四态聚合（临时 HOME）。"""

from datetime import datetime, timedelta

from trae_checkin import constants, history, jsonstore


def _dstr(today, back):
    return (today - timedelta(days=back)).strftime("%Y-%m-%d")


def _item(ok, user="alice", note=""):
    return {
        "platform": constants.PLAT_TRAEWORK,
        "platform_label": "TraeWork CN",
        "username": user,
        "note": note,
        "ok": ok,
        "already": False,
        "credits": 1,
        "message": "",
        "time": "09:00",
    }


def test_history_calendar_four_states(data_home):
    today = datetime.now().date()

    def dstr(back):
        return _dstr(today, back)

    data = {
        "days": {
            dstr(0): {"a": _item(True)},
            dstr(1): {"a": _item(True), "b": _item(False, "bob")},
            dstr(2): {"a": _item(False)},
            dstr(10): {"a": _item(True, note="主力号")},
        }
    }
    jsonstore._save_json_file(history.HISTORY_FILE, data)

    cal = history.history_calendar(90)

    assert len(cal) == 90
    assert cal[dstr(0)]["state"] == "all_ok" and cal[dstr(0)]["success"] == 1
    assert cal[dstr(1)]["state"] == "partial"
    assert cal[dstr(1)]["success"] == 1 and cal[dstr(1)]["fail"] == 1
    assert cal[dstr(2)]["state"] == "all_fail" and cal[dstr(2)]["fail"] == 1
    assert cal[dstr(3)]["state"] == "none" and cal[dstr(3)]["total"] == 0
    assert cal[dstr(10)]["items"][0]["note"] == "主力号"
    assert dstr(89) in cal and dstr(90) not in cal
