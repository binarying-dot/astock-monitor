#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股全市场下跌家数监控 · GitHub Actions 版
==========================================
由 GitHub Actions 定时调度（工作日北京时间 9:25-15:00，每 5 分钟）。
零第三方依赖（纯标准库），状态通过仓库内 state.json 持久化。

环境变量：
    WECOM_WEBHOOK   企业微信群机器人 Webhook（GitHub Secrets 中配置）
    THRESHOLD       下跌家数告警阈值，默认 5000
"""

import datetime as dt
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

THRESHOLD = int(os.environ.get("THRESHOLD", "5000"))
WECOM_WEBHOOK = (os.environ.get("WECOM_WEBHOOK") or "").strip()
FORCE = (os.environ.get("FORCE") or "").strip().lower() in ("1", "true", "yes")
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# ---- 会话循环参数 ----------------------------------------------------------
# GitHub 的 cron 在免费账号上会被大幅延迟/丢弃（实测每 1-2 小时才真正触发一次），
# 因此不能依赖「每 5 分钟触发一次」。改成：只要被触发一次，就在这一个 job 内
# 自行循环检测，直到收盘为止。这样触发频率再低也能覆盖大半个交易日。
INTERVAL_MIN = int(os.environ.get("INTERVAL_MIN", "5"))     # 循环内检测间隔（分钟）
LOOP_ENABLED = (os.environ.get("LOOP", "1").strip().lower() not in ("0", "false", "no"))


def _hhmm(value, default):
    try:
        h, m = str(value).split(":")
        return dt.time(int(h), int(m))
    except Exception:
        return default


SESSION_START = _hhmm(os.environ.get("SESSION_START"), dt.time(9, 25))   # 开始检测
SESSION_END = _hhmm(os.environ.get("SESSION_END"), dt.time(15, 0))       # 收盘
READY_FROM = _hhmm(os.environ.get("READY_FROM"), dt.time(9, 15))         # 早于此时刻触发则退出
LUNCH_START = dt.time(11, 30)
LUNCH_END = dt.time(13, 0)
MAX_LOOP_SECONDS = 345 * 60         # 单次 job 最长循环 5h45m（GitHub 单 job 上限 6h）
# 仅用于验证「循环本身」的低成本测试模式：跳过时段闸门，按上面的自定义窗口循环
LOOP_TEST = (os.environ.get("LOOP_TEST") or "").strip().lower() in ("1", "true", "yes")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PAGE_SIZE = 100
MAX_WORKERS = 12
EM_FS_MAIN = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"
EM_FS_ALL = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

# 东财数据源在本工作流环境若不可用会自动切到新浪
SOURCES_ORDER = ["eastmoney", "sina"]


# --------------------------------------------------------------------------
def now_cn():
    """GitHub runner 是 UTC，统一换算成北京时间。"""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) + dt.timedelta(hours=8)


def log(msg):
    print("[%s] %s" % (now_cn().strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def http_get(url, referer=None, timeout=12, encoding="utf-8"):
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(encoding, errors="replace")


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def write_summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except Exception:
            pass


# --------------------------------------------------------------------------
def is_trading_day_now():
    """用腾讯指数行情的时间戳判断今天是否交易日，取不到时退化为非周末。"""
    try:
        txt = http_get("https://qt.gtimg.cn/q=sh000001", encoding="gbk", timeout=8)
        parts = txt.split('"')[1].split("~") if '"' in txt else []
        ts = parts[30] if len(parts) > 30 else ""
        if len(ts) >= 8 and ts[:8].isdigit():
            return ts[:8] == now_cn().strftime("%Y%m%d")
    except Exception:
        pass
    return now_cn().weekday() < 5


# --------------------------------------------------------------------------
# 数据源 1：东方财富
# --------------------------------------------------------------------------
def _em_page(pn, fs, retries=3):
    url = ("https://push2.eastmoney.com/api/qt/clist/get?pn=%d&pz=%d&po=1&np=1"
           "&fltt=2&invt=2&fid=f3&fs=%s&fields=f3,f12"
           % (pn, PAGE_SIZE, urllib.parse.quote(fs, safe=":,+")))
    last = None
    for _ in range(retries):
        try:
            return json.loads(http_get(
                url, referer="https://quote.eastmoney.com/center/gridlist.html"))
        except Exception as e:
            last = e
            time.sleep(0.4)
    raise RuntimeError("东财第 %d 页: %s" % (pn, last))


def snap_eastmoney():
    first = _em_page(1, EM_FS_ALL)
    data = first.get("data") or {}
    total = int(data.get("total") or 0)
    rows = list(data.get("diff") or [])
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if pages > 1:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for fut in [ex.submit(_em_page, p, EM_FS_ALL) for p in range(2, pages + 1)]:
                try:
                    rows.extend((fut.result().get("data") or {}).get("diff") or [])
                except Exception:
                    pass
    up = down = flat = 0
    for r in rows:
        v = r.get("f3")
        if v is None or v in ("-", ""):
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if v > 0:
            up += 1
        elif v < 0:
            down += 1
        else:
            flat += 1
    return total, up, down, flat


# --------------------------------------------------------------------------
# 数据源 2：新浪财经
# --------------------------------------------------------------------------
SINA_BASE = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
             "Market_Center.getHQNodeData")
SINA_REF = "https://finance.sina.com.cn/"


def _sina_page(pn, retries=3):
    url = ("%s?page=%d&num=%d&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=page"
           % (SINA_BASE, pn, PAGE_SIZE))
    last = None
    for _ in range(retries):
        try:
            data = json.loads(http_get(url, referer=SINA_REF, encoding="gbk"))
            return data if isinstance(data, list) else []
        except Exception as e:
            last = e
            time.sleep(0.4)
    raise RuntimeError("新浪第 %d 页: %s" % (pn, last))


def snap_sina():
    total = int(http_get(
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        "Market_Center.getHQNodeStockCount?node=hs_a",
        referer=SINA_REF, encoding="gbk").strip().strip('"') or 0)
    rows = list(_sina_page(1))
    pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    if pages > 1:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            for fut in [ex.submit(_sina_page, p) for p in range(2, pages + 1)]:
                try:
                    rows.extend(fut.result())
                except Exception:
                    pass
    up = down = flat = 0
    for r in rows:
        try:
            if float(r.get("trade") or 0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        try:
            cp = float(r.get("changepercent"))
        except (TypeError, ValueError):
            continue
        if cp > 0:
            up += 1
        elif cp < 0:
            down += 1
        else:
            flat += 1
    return total, up, down, flat


def snapshot():
    errors = []
    for name in SOURCES_ORDER:
        fn = {"eastmoney": snap_eastmoney, "sina": snap_sina}.get(name)
        t0 = time.time()
        try:
            total, up, down, flat = fn()
        except Exception as e:
            errors.append("%s: %s" % (name, e))
            continue
        traded = up + down + flat
        if total <= 0 or traded <= 0 or traded < total * 0.9:
            errors.append("%s: 数据不完整（有效 %d / 总数 %d）" % (name, traded, total))
            continue
        return {
            "source": name,
            "time": now_cn().strftime("%Y-%m-%d %H:%M:%S"),
            "total": total, "traded": traded,
            "up": up, "down": down, "flat": flat,
            "down_ratio": round(down / traded * 100, 2),
            "elapsed": round(time.time() - t0, 2),
        }
    raise RuntimeError("全部数据源不可用 -> " + " ｜ ".join(errors))


# --------------------------------------------------------------------------
def push_wecom(s):
    if not WECOM_WEBHOOK:
        return False, "未配置 WECOM_WEBHOOK"
    content = (
        "## ⚠️ A股下跌家数突破 %d 家\n"
        "**下跌家数**：<font color=\"warning\">%d 家</font>\n"
        "**上涨家数**：%d 家\n"
        "**平盘家数**：%d 家\n"
        "**下跌占比**：<font color=\"warning\">%.2f%%</font>（有效交易 %d 只）\n"
        "**检测时间**：%s\n"
        "> 数据来源：%s｜沪深京全市场，剔除停牌\n"
        "> 由 GitHub Actions 云端监控自动发出"
        % (THRESHOLD, s["down"], s["up"], s["flat"], s["down_ratio"], s["traded"],
           s["time"], "东方财富" if s["source"] == "eastmoney" else "新浪财经")
    )
    body = json.dumps({"msgtype": "markdown", "markdown": {"content": content}}).encode("utf-8")
    req = urllib.request.Request(WECOM_WEBHOOK, data=body,
                                 headers={"Content-Type": "application/json", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=12) as r:
            res = json.loads(r.read().decode("utf-8"))
        if res.get("errcode") == 0:
            return True, "企业微信推送成功"
        return False, "企业微信返回异常: %s" % res
    except Exception as e:
        return False, "企业微信推送失败: %s" % e


def run_check(today):
    """执行一次检测：抓取 → 判断 → 按需推送 → 写状态。返回快照或 None。"""
    try:
        s = snapshot()
    except Exception as e:
        log("抓取失败: %s" % e)
        write_summary("⚠️ 行情抓取失败：%s" % e)
        return None

    log("[%s] 涨 %d / 跌 %d / 平 %d ｜ 下跌占比 %.2f%% ｜ 耗时 %.2fs"
        % (s["source"], s["up"], s["down"], s["flat"], s["down_ratio"], s["elapsed"]))

    write_summary(
        "| 指标 | 数值 |\n|---|---|\n"
        "| 检测时间 | %s |\n| 数据源 | %s |\n| 下跌家数 | **%d** |\n"
        "| 上涨家数 | %d |\n| 下跌占比 | %.2f%% |\n| 阈值 | %d |"
        % (s["time"], s["source"], s["down"], s["up"], s["down_ratio"], THRESHOLD))

    state = load_state()
    state_changed = False
    if state.get("date") != today:
        state = {"date": today, "pushed": False}
        state_changed = True
        log("今日首次检测，状态已重置")

    state["last_check"] = s["time"]
    state["last_down"] = s["down"]
    state["checks"] = int(state.get("checks") or 0) + 1
    state_changed = True

    if s["down"] >= THRESHOLD:
        if state.get("pushed"):
            log("今日已告警过，跳过（当前跌 %d 家）" % s["down"])
        else:
            ok, msg = push_wecom(s)
            log(msg)
            if ok:
                state["pushed"] = True
                state["pushed_at"] = s["time"]
                state["down_at_push"] = s["down"]
    else:
        log("结果: 未触发（跌 %d 家 < 阈值 %d 家）" % (s["down"], THRESHOLD))

    if state_changed:
        save_state(state)             # 交给 workflow 步骤 commit
    return s


def main():
    t = now_cn()
    today = t.strftime("%Y-%m-%d")

    # ---- 测试模式：跳过闸门、只跑一次 ----
    if FORCE:
        log("⚠️ FORCE 测试模式：跳过时段闸门与循环，仅检测一次")
        run_check(today)
        return 0

    if LOOP_TEST:
        log("⚠️ LOOP_TEST 模式：跳过时段闸门，按自定义窗口循环"
            "（%s - %s，间隔 %d 分钟）"
            % (SESSION_START.strftime("%H:%M"), SESSION_END.strftime("%H:%M"), INTERVAL_MIN))
        return session_loop(today)

    # ---- 时段闸门 ----
    if t.weekday() >= 5:
        log("周末休市，退出")
        return 0
    tt = t.time()
    if tt > SESSION_END:
        log("已收盘（%s），退出" % t.strftime("%H:%M"))
        return 0
    if tt < READY_FROM:
        log("尚未进入监控窗口（%s），退出" % t.strftime("%H:%M"))
        return 0
    if not is_trading_day_now():
        log("今日为节假日休市，退出")
        return 0

    # 9:15-9:25 之间被触发：等到 9:25 再开始，以覆盖整个时段
    if tt < SESSION_START:
        wait_s = (dt.datetime.combine(t.date(), SESSION_START) - t).total_seconds()
        log("早于开盘 %.1f 分钟，等待至 %s 开始"
            % (wait_s / 60.0, SESSION_START.strftime("%H:%M")))
        time.sleep(max(0.0, wait_s))

    if not LOOP_ENABLED:
        run_check(today)
        return 0

    return session_loop(today)


def session_loop(today):
    """一次触发后循环检测到收盘，避免受 GitHub cron 触发频率限制。"""
    loop_t0 = time.time()
    log("进入会话循环：每 %d 分钟检测一次，直至 %s"
        % (INTERVAL_MIN, SESSION_END.strftime("%H:%M")))
    rounds = 0
    while now_cn().time() <= SESSION_END:
        if time.time() - loop_t0 > MAX_LOOP_SECONDS:
            log("已达单次会话时长上限，本轮结束（下次触发将继续覆盖）")
            break

        if not LOOP_TEST and LUNCH_START < now_cn().time() < LUNCH_END:
            log("午间休市（%s），%d 分钟后继续" % (now_cn().strftime("%H:%M"), INTERVAL_MIN))
            time.sleep(INTERVAL_MIN * 60)
            continue

        rounds += 1
        log("---- 第 %d 次检测 ----" % rounds)
        run_check(today)

        if now_cn().time() >= SESSION_END:
            break
        time.sleep(INTERVAL_MIN * 60)

    log("会话结束：本次 job 共检测 %d 次，耗时 %.1f 分钟"
        % (rounds, (time.time() - loop_t0) / 60.0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
