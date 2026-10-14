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
    return dt.datetime.utcnow() + dt.timedelta(hours=8)


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


def main():
    t = now_cn()
    today = t.strftime("%Y-%m-%d")

    # ---- 时段闸门（工作日 9:25-15:00，午休跳过；FORCE=1 时为测试跳过）----
    if not FORCE:
        if t.weekday() >= 5:
            log("周末休市，退出")
            return 0
        tt = t.time()
        if not (dt.time(9, 25) <= tt <= dt.time(15, 0)):
            log("非监控时段（%s），退出" % t.strftime("%H:%M"))
            return 0
        if dt.time(11, 30) < tt < dt.time(13, 0):
            log("午间休市，退出")
            return 0
        if not is_trading_day_now():
            log("今日为节假日休市，退出")
            return 0
    else:
        log("⚠️ FORCE 测试模式：跳过时段闸门")

    # ---- 抓取 ----
    try:
        s = snapshot()
    except Exception as e:
        log("抓取失败: %s" % e)
        write_summary("⚠️ 行情抓取失败：%s" % e)
        return 0                      # 不让 Actions 报红，避免噪音邮件

    log("[%s] 涨 %d / 跌 %d / 平 %d ｜ 下跌占比 %.2f%% ｜ 耗时 %.2fs"
        % (s["source"], s["up"], s["down"], s["flat"], s["down_ratio"], s["elapsed"]))

    write_summary(
        "| 指标 | 数值 |\n|---|---|\n"
        "| 检测时间 | %s |\n| 数据源 | %s |\n| 下跌家数 | **%d** |\n"
        "| 上涨家数 | %d |\n| 下跌占比 | %.2f%% |\n| 阈值 | %d |"
        % (s["time"], s["source"], s["down"], s["up"], s["down_ratio"], THRESHOLD))

    # ---- 状态与告警 ----
    state = load_state()
    state_changed = False
    first_run_today = state.get("date") != today
    triggered = s["down"] >= THRESHOLD

    if first_run_today:
        state = {"date": today, "pushed": False}
        state_changed = True
        log("今日首次检测，状态已重置")

    action = "未触发"
    if triggered:
        if state.get("pushed"):
            action = "今日已告警过，跳过"
            log(action)
        else:
            ok, msg = push_wecom(s)
            log(msg)
            action = msg
            if ok:
                state["pushed"] = True
                state["pushed_at"] = s["time"]
                state["down_at_push"] = s["down"]
                state_changed = True

    if state_changed:
        save_state(state)             # 交给 workflow 步骤 commit，同时保活仓库

    log("结果: %s" % action)
    return 0


if __name__ == "__main__":
    sys.exit(main())
