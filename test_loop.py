#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""模拟时钟验证 monitor_actions 的会话循环逻辑（不联网、不推送、不写文件）。"""
import datetime as dt
import os

os.environ["FORCE"] = ""
os.environ["THRESHOLD"] = "5000"

import monitor_actions as M

STATE = {}


class Clock:
    def __init__(self, start):
        self.t = start
        self.wall = 0.0

    def now(self):
        return self.t

    def advance(self, sec):
        self.t += dt.timedelta(seconds=sec)
        self.wall += sec


class FakeTime:
    def __init__(self, clock):
        self.c = clock

    def sleep(self, s):
        self.c.advance(s)

    def time(self):
        return self.c.wall


def run_case(label, start, down, exp_push, exp_checks_note=""):
    clock = Clock(start)
    pushes, checks = [], []

    M.FORCE = False
    M.THRESHOLD = 5000
    M.INTERVAL_MIN = 5
    M.LOOP_ENABLED = True
    M.now_cn = clock.now
    M.time = FakeTime(clock)
    M.load_state = lambda: dict(STATE)
    M.save_state = lambda s: STATE.update(s)
    M.write_summary = lambda x: None
    M.is_trading_day_now = lambda: True      # 交易日判断另行单独验证，这里固定为交易日
    M.snapshot = lambda: {
        "source": "eastmoney", "time": M.now_cn().strftime("%Y-%m-%d %H:%M:%S"),
        "total": 5570, "traded": 5570, "up": 5570 - down - 0, "down": down, "flat": 0,
        "down_ratio": round(down / 5570.0 * 100, 2), "elapsed": 2.0,
    }
    M.push_wecom = lambda s: (pushes.append(s["time"]), (True, "推送成功"))[1]

    orig = M.run_check

    def spy(today):
        r = orig(today)
        checks.append(M.now_cn().strftime("%H:%M"))
        return r

    M.run_check = spy
    STATE.clear()
    try:
        M.main()
    finally:
        M.run_check = orig

    span = ("%s → %s" % (checks[0], checks[-1])) if checks else "无检测"
    flag = "✅" if len(pushes) == exp_push else "❌"
    print("  %s %-28s 检测 %2d 次 [%s]  推送 %d 次（预期 %d）%s"
          % (flag, label, len(checks), span, len(pushes), exp_push, exp_checks_note))
    return len(pushes) == exp_push


print("=" * 82)
print("会话循环逻辑验证（模拟时钟 · 阈值 5000 · 下跌 5100 家）")
print("=" * 82)
allok = True
allok &= run_case("周一 09:14 触发（过早，退出）", dt.datetime(2026, 10, 19, 9, 14), 5100, 0)
allok &= run_case("周一 09:16 触发（等到 9:25）", dt.datetime(2026, 10, 19, 9, 16), 5100, 1)
allok &= run_case("周一 09:30 触发（完整时段）", dt.datetime(2026, 10, 19, 9, 30), 5100, 1)
allok &= run_case("周一 11:00 触发（跨午休）", dt.datetime(2026, 10, 19, 11, 0), 5100, 1)
allok &= run_case("周一 14:40 触发（尾盘）", dt.datetime(2026, 10, 19, 14, 40), 5100, 1)
allok &= run_case("周六 10:00 触发（休市，退出）", dt.datetime(2026, 10, 17, 10, 0), 5100, 0)
allok &= run_case("周一 15:30 触发（已收盘，退出）", dt.datetime(2026, 10, 19, 15, 30), 5100, 0)
print("-" * 82)
allok &= run_case("周一 09:30 触发（未触发，不推送）", dt.datetime(2026, 10, 19, 9, 30), 2000, 0)
print("=" * 82)
print("全部通过" if allok else "存在失败用例")
