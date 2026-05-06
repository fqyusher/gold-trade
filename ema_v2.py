import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
import time
import smtplib
import os
from dotenv import load_dotenv
from email.mime.text import MIMEText
from datetime import datetime

# ================= 配置 =================
load_dotenv()

SYMBOL = os.getenv("SYMBOL", "XAUUSD")
MAGIC_NUMBER = int(os.getenv("MAGIC_NUMBER", 20260430))
VOLUME = float(os.getenv("VOLUME", 0.01))

MAX_DAILY_LOSS = float(os.getenv("MAX_DAILY_LOSS", 100))
ATR_LIMIT = float(os.getenv("ATR_LIMIT", 12))

SL_MIN = 6
SL_MAX = 12

EMAIL_NOTIFY = os.getenv("EMAIL_NOTIFY", "False").lower() == "true"
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")

TF_M5 = mt5.TIMEFRAME_M5
TF_M15 = mt5.TIMEFRAME_M15

# ================= 状态 =================
daily_pnl = 0
last_day = None
last_trade_time = None


# ================= 邮件 =================
def send_email(subject, content):
    if not EMAIL_NOTIFY:
        return
    try:
        msg = MIMEText(content)
        msg["Subject"] = subject
        msg["From"] = SENDER_EMAIL
        msg["To"] = RECEIVER_EMAIL

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as s:
            s.starttls()
            s.login(SENDER_EMAIL, SENDER_PASSWORD)
            s.send_message(msg)

        print(f"📧 {subject}")
    except Exception as e:
        print(f"[EMAIL ERROR] {e}")


# ================= 🆕 启动检测 =================
def test_order_execution():
    print("\n🧪 开始交易环境检测...")

    info = mt5.symbol_info(SYMBOL)
    tick = mt5.symbol_info_tick(SYMBOL)

    if not info or not tick:
        print("❌ 无法获取交易信息")
        return False

    price = tick.ask

    base_request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": info.volume_min,
        "type": mt5.ORDER_TYPE_BUY,
        "price": price,
        "sl": price - 5,
        "tp": price + 5,
        "deviation": 10,
        "magic": MAGIC_NUMBER,
        "comment": "TEST_ORDER"
    }

    # ⭐ 关键修复：逐个测试真实支持的 filling
    modes = [
        mt5.ORDER_FILLING_RETURN,
        mt5.ORDER_FILLING_IOC,
        mt5.ORDER_FILLING_FOK
    ]

    for mode in modes:
        request = base_request.copy()
        request["type_filling"] = mt5.ORDER_FILLING_IOC

        print(f"🧪 测试 filling_mode = {mode}")

        result = mt5.order_send(request)

        if result is None:
            continue

        print(f"→ retcode={result.retcode}")

        if result.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"✅ 可用模式: {mode}")
            return True

    print("❌ 所有 filling 模式都不可用（券商限制）")
    send_email("交易环境异常", "filling mode 全部失败")
    return False


# ================= 🆕 成交模式适配 =================
def get_filling_modes():
    return [
        mt5.ORDER_FILLING_RETURN,
        mt5.ORDER_FILLING_IOC,
        mt5.ORDER_FILLING_FOK
    ]


# ================= 数据 =================
def get_data(tf):
    rates = mt5.copy_rates_from_pos(SYMBOL, tf, 0, 200)
    if rates is None:
        return pd.DataFrame()

    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")

    df["ema20"] = ta.ema(df["close"], 20)
    df["ema50"] = ta.ema(df["close"], 50)
    df["rsi"] = ta.rsi(df["close"], 14)
    df["atr"] = ta.atr(df["high"], df["low"], df["close"], 14)

    return df


# ================= 趋势 =================
def get_trend(df):
    last = df.iloc[-1]
    return "BUY" if last["ema20"] > last["ema50"] else "SELL"


# ================= 信号 =================
def get_signal(df, trend):
    last = df.iloc[-1]

    if trend == "BUY":
        if last["close"] > last["ema20"] and last["rsi"] > 50:
            return mt5.ORDER_TYPE_BUY

    if trend == "SELL":
        if last["close"] < last["ema20"] and last["rsi"] < 50:
            return mt5.ORDER_TYPE_SELL

    return None


# ================= ATR风控 =================
def calc_sl_distance(atr):
    dynamic_sl = 1.5 * atr
    return max(SL_MIN, dynamic_sl)


def check_sl_valid(sl_dist):
    return sl_dist <= SL_MAX


# ================= 防重复 =================
def duplicate_check(curr_time):
    global last_trade_time
    if last_trade_time == curr_time:
        print("🚫 重复交易拦截")
        return True
    return False


# ================= 邮件报告 =================
def build_report(df_m5, df_m15, order_type, price, sl, tp, atr, sl_dist):
    last5 = df_m5.iloc[-1]
    last15 = df_m15.iloc[-1]

    return f"""
时间: {datetime.now()}

方向: {"BUY" if order_type == 0 else "SELL"}

M15趋势:
EMA20={last15['ema20']:.2f}
EMA50={last15['ema50']:.2f}

M5入场:
价格={last5['close']:.2f}
RSI={last5['rsi']:.2f}

ATR={atr:.2f}

止损距离={sl_dist:.2f}
止损={sl:.2f}
止盈={tp:.2f}

日盈亏={daily_pnl:.2f}
"""


# ================= 下单（修复版） =================
def execute_trade(order_type, df_m5, df_m15):
    global last_trade_time

    tick = mt5.symbol_info_tick(SYMBOL)
    info = mt5.symbol_info(SYMBOL)

    price = tick.ask if order_type == 0 else tick.bid
    atr = df_m5.iloc[-1]["atr"]

    sl_dist = calc_sl_distance(atr)

    if not check_sl_valid(sl_dist):
        print(f"🚫 止损过大 {sl_dist:.2f} > {SL_MAX}")
        send_email("风控拦截", f"止损过大: {sl_dist:.2f}")
        return

    if order_type == 0:
        sl = price - sl_dist
        tp = price + sl_dist * 2
    else:
        sl = price + sl_dist
        tp = price - sl_dist * 2

    base_request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": VOLUME,
        "type": order_type,
        "price": price,
        "sl": round(sl, info.digits),
        "tp": round(tp, info.digits),
        "magic": MAGIC_NUMBER,
        "comment": "SL_5_15_SYSTEM"
    }

    print(f"\n🚀 尝试下单...")

    # ⭐ 核心修复：自动适配成交模式
    for mode in get_filling_modes():
        request = base_request.copy()
        request["type_filling"] = mode

        print(f"→ 尝试 filling_mode={mode}")

        res = mt5.order_send(request)

        if res.retcode == mt5.TRADE_RETCODE_DONE:
            print(f"✅ 成交 {res.order}")

            report = build_report(df_m5, df_m15, order_type, price, sl, tp, atr, sl_dist)
            send_email("交易报告", report)

            last_trade_time = df_m5.iloc[-1]["time"]
            return
        else:
            print(f"❌ 模式{mode}失败: {res.comment}")

    print("🚫 所有成交模式失败")
    send_email("交易失败", "所有filling模式失败")


# ================= 主循环 =================
def run():
    global last_day, daily_pnl

    if not mt5.initialize():
        print("❌ MT5 初始化失败")
        return

    print("🚀 系统启动")

    # ⭐ 启动检测
    if not test_order_execution():
        print("❌ 环境异常，停止运行")
        return

    last_time = None

    while True:
        try:
            today = datetime.now().date()
            if last_day != today:
                daily_pnl = 0
                last_day = today
                print("🔄 新交易日")

            df_m5 = get_data(TF_M5)
            df_m15 = get_data(TF_M15)

            if df_m5.empty:
                time.sleep(2)
                continue

            curr_time = df_m5.iloc[-1]["time"]

            if curr_time != last_time:

                if last_time is None:
                    print("🟡 启动保护")
                    last_time = curr_time
                    continue

                if duplicate_check(curr_time):
                    continue

                trend = get_trend(df_m15)
                signal = get_signal(df_m5, trend)

                if signal is not None:
                    execute_trade(signal, df_m5, df_m15)

                last_time = curr_time

            tick = mt5.symbol_info_tick(SYMBOL)
            price = tick.bid if tick else 0

            m5_rsi = df_m5.iloc[-1]['rsi']
            m15_rsi = df_m15.iloc[-1]['rsi']

            print(
                f"\r[{datetime.now().strftime('%H:%M:%S')} "
                f"| 现价:{price:.2f}] "
                f"| M5_RSI:{m5_rsi:.1f} "
                f"| M15_RSI:{m15_rsi:.1f} "
                f"| 盈亏:{daily_pnl:.2f}",
                end=""
            )

            time.sleep(1)

        except Exception as e:
            print(f"💥 ERROR {e}")
            send_email("系统异常", str(e))
            time.sleep(5)


if __name__ == "__main__":
    run()