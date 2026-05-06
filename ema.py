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
ATR_LIMIT_UP = float(os.getenv("ATR_LIMIT_UP", 12))
ATR_LIMIT_DOWN = float(os.getenv("ATR_LIMIT_DOWN", 6))

SL_MIN = 6
SL_MAX = 13

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
last_trade_timestamp = 0
TRADE_COOLDOWN = 300  # 5分钟冷却


# ================= 邮件（修复连接问题） =================
def send_email(subject, content):
    if not EMAIL_NOTIFY:
        return

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as s:
            s.ehlo()
            s.starttls()
            s.ehlo()
            s.login(SENDER_EMAIL, SENDER_PASSWORD)

            msg = MIMEText(content)
            msg["Subject"] = subject
            msg["From"] = SENDER_EMAIL
            msg["To"] = RECEIVER_EMAIL

            s.send_message(msg)

        print(f"📧 {subject}")

    except Exception as e:
        print(f"[EMAIL ERROR] {e}")


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
    return max(ATR_LIMIT_DOWN, dynamic_sl)


def check_sl_valid(sl_dist):
    return sl_dist <= SL_MAX


# ================= 防重复（核心修复） =================
def duplicate_check(curr_time):
    global last_trade_time, last_trade_timestamp

    now = time.time()

    if last_trade_time == curr_time:
        print("🚫 同K线重复交易")
        return True

    if now - last_trade_timestamp < TRADE_COOLDOWN:
        print("🚫 冷却未完成")
        return True

    last_trade_timestamp = now
    return False


# ================= 盈亏更新 =================
def update_pnl():
    global daily_pnl

    positions = mt5.positions_get(symbol=SYMBOL)
    if positions:
        daily_pnl = sum(p.profit for p in positions)
    else:
        daily_pnl = 0


# ================= 报告 =================
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

当前盈亏={daily_pnl:.2f}
"""


# ================= 下单 =================
def execute_trade(order_type, df_m5, df_m15):
    global last_trade_time

    # 🚫 持仓锁（防重开）
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    if positions and len(positions) > 0:
        print("🚫 已有持仓，禁止重复开仓")
        return

    tick = mt5.symbol_info_tick(SYMBOL)
    info = mt5.symbol_info(SYMBOL)

    price = tick.ask if order_type == 0 else tick.bid
    atr = df_m5.iloc[-1]["atr"]

    sl_dist = calc_sl_distance(atr)

    if not check_sl_valid(sl_dist):
        print(f"🚫 止损过大 {sl_dist:.2f}")
        send_email("风控拦截", f"止损过大 {sl_dist:.2f}")
        return

    if order_type == 0:
        sl = price - sl_dist
        tp = price + sl_dist * 2
    else:
        sl = price + sl_dist
        tp = price - sl_dist * 2

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": SYMBOL,
        "volume": VOLUME,
        "type": order_type,
        "price": price,
        "sl": round(sl, info.digits),
        "tp": round(tp, info.digits),
        "magic": MAGIC_NUMBER,
        "deviation": 10,
        "comment": "SL_5_15_SYSTEM",
        "type_filling": mt5.ORDER_FILLING_IOC
    }

    res = mt5.order_send(request)

    if res.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"✅ 成交 {res.order}")

        report = build_report(df_m5, df_m15, order_type, price, sl, tp, atr, sl_dist)
        send_email("交易报告", report)

        last_trade_time = df_m5.iloc[-1]["time"]

    else:
        print(f"❌ 下单失败: {res.comment}")


# ================= 主循环 =================
def run():
    global last_day

    if not mt5.initialize():
        print("❌ MT5 初始化失败")
        return

    if not mt5.symbol_select(SYMBOL, True):
        print("❌ 品种不可用")
        return

    print("🚀 系统启动")

    last_time = None

    while True:
        try:
            today = datetime.now().date()

            if last_day != today:
                last_day = today
                print("🔄 新交易日")

            df_m5 = get_data(TF_M5)
            df_m15 = get_data(TF_M15)

            if df_m5.empty:
                time.sleep(2)
                continue

            curr_time = df_m5.iloc[-1]["time"]

            update_pnl()

            if curr_time != last_time:

                if last_time is None:
                    print("🟡 初始化保护")
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

            print(
                f"\r[{datetime.now().strftime('%H:%M:%S')} "
                f"| 现价:{price:.2f}] "
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