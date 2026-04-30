import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
import time
import smtplib
import os
from dotenv import load_dotenv
from email.mime.text import MIMEText
from datetime import datetime

# ================= 加载配置 =================
load_dotenv()

# 交易参数
SYMBOL = os.getenv("SYMBOL", "GOLD_")
MAGIC_NUMBER = int(os.getenv("MAGIC_NUMBER", 20260430))
VOLUME = float(os.getenv("VOLUME", 0.01))
SL_USD = float(os.getenv("SL_USD", 8.0))
TP_USD = float(os.getenv("TP_USD", 16.0))
BE_PROFIT = float(os.getenv("BE_PROFIT", 2.0))
TRAIL_STEP = float(os.getenv("TRAIL_STEP", 2.0))
MAX_TOTAL_POSITIONS = int(os.getenv("MAX_POSITIONS", 5))

# 邮件配置
EMAIL_NOTIFY = os.getenv("EMAIL_NOTIFY", "False").lower() == "true"
SMTP_SERVER = os.getenv("SMTP_SERVER")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SENDER_EMAIL = os.getenv("SENDER_EMAIL")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")

MONITOR_TFS = {
    "5M": mt5.TIMEFRAME_M5,
    "15M": mt5.TIMEFRAME_M15,
    "30M": mt5.TIMEFRAME_M30,
    "1H": mt5.TIMEFRAME_H1,
    "4H": mt5.TIMEFRAME_H4
}

# ================= 核心功能函数 =================

def send_email_notification(subject, content):
    """发送邮件通知"""
    if not EMAIL_NOTIFY: return
    try:
        msg = MIMEText(content)
        msg['Subject'] = subject
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_EMAIL
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
    except Exception as e:
        print(f"\n[邮件错误]: {e}")

def get_processed_data(tf):
    """获取并计算指标数据"""
    rates = mt5.copy_rates_from_pos(SYMBOL, tf, 0, 150)
    if rates is None or len(rates) < 50: return pd.DataFrame()

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.rsi(length=14, append=True)

    df.rename(columns={
        'MACD_12_26_9': 'macd', 'MACDs_12_26_9': 'signal',
        'MACDh_12_26_9': 'hist', 'RSI_14': 'rsi'
    }, inplace=True)
    return df

def get_signal_type(df, order_type):
    """判断信号模式"""
    if df.empty or len(df) < 20: return None
    curr, prev = df.iloc[-1], df.iloc[-2]
    rsi_hist = df['rsi'].tail(8)

    if order_type == mt5.ORDER_TYPE_BUY:
        # 反转模式
        if rsi_hist.min() <= 35 and curr['hist'] > prev['hist'] and curr['rsi'] < 60:
            return "REVERSAL"
        # 顺势模式
        if curr['macd'] > 0 and curr['rsi'] > 52 and curr['hist'] > prev['hist']:
            return "TREND"

    elif order_type == mt5.ORDER_TYPE_SELL:
        # 反转模式
        if rsi_hist.max() >= 65 and curr['hist'] < prev['hist'] and curr['rsi'] > 40:
            return "REVERSAL"
        # 顺势模式
        if curr['macd'] < 0 and curr['rsi'] < 48 and curr['hist'] < prev['hist']:
            return "TREND"

    return None

def manage_trailing_logic():
    """实时追踪止损维护"""
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    if not positions: return
    for pos in positions:
        entry, curr, sl = pos.price_open, pos.price_current, pos.sl
        # 盈亏金额 = 点差 * 100 * 手数
        profit_usd = (curr - entry) * 100 * pos.volume if pos.type == 0 else (entry - curr) * 100 * pos.volume

        new_sl = 0
        if pos.type == 0: # 多单
            if profit_usd >= BE_PROFIT and (sl < entry or sl == 0):
                new_sl = entry + 0.05
            elif profit_usd >= TRAIL_STEP * 2:
                target = entry + (int(profit_usd // TRAIL_STEP) - 1) * (TRAIL_STEP / (pos.volume * 100))
                if target > sl + 0.1: new_sl = target
        else: # 空单
            if profit_usd >= BE_PROFIT and (sl > entry or sl == 0):
                new_sl = entry - 0.05
            elif profit_usd >= TRAIL_STEP * 2:
                target = entry - (int(profit_usd // TRAIL_STEP) - 1) * (TRAIL_STEP / (pos.volume * 100))
                if target < sl - 0.1: new_sl = target

        if new_sl > 0:
            mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "sl": round(new_sl, 2), "tp": pos.tp})

def execute_trade(order_type, tf_name, signal_mode):
    """执行下单任务"""
    # 获取当前所有持仓
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)

    # 唯一性检查：防止同一周期同一模式重复下单
    current_comment = f"{signal_mode}:{tf_name}"
    if positions:
        if len(positions) >= MAX_TOTAL_POSITIONS: return
        for pos in positions:
            if pos.comment == current_comment: return # 已有相同信号持仓，拦截

    tick = mt5.symbol_info_tick(SYMBOL)
    info = mt5.symbol_info(SYMBOL)
    if not tick or not info: return

    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    sl = round(price - SL_USD, info.digits) if order_type == 0 else round(price + SL_USD, info.digits)
    tp = round(price + TP_USD, info.digits) if order_type == 0 else round(price - TP_USD, info.digits)

    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": VOLUME,
        "type": order_type, "price": price, "sl": sl, "tp": tp,
        "magic": MAGIC_NUMBER, "comment": current_comment,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    res = mt5.order_send(request)
    if res.retcode == mt5.TRADE_RETCODE_DONE:
        side = 'BUY' if order_type == 0 else 'SELL'
        print(f"\n✅ 成交: {current_comment} | 价格: {price}")
        send_email_notification(f"交易提醒: {side} {SYMBOL}", f"周期: {tf_name}\n模式: {signal_mode}\n价格: {price}")

# ================= 主程序循环 =================

def run_loop():
    if not mt5.initialize():
        print("MT5 初始化失败")
        return

    print("-" * 50)
    print(f"🤖 系统启动成功 | 时间: {datetime.now().strftime('%H:%M:%S')}")

    # --- 初始化时间对齐，防止重启重复下单 ---
    last_processed_times = {}
    for name, tf_code in MONITOR_TFS.items():
        df = get_processed_data(tf_code)
        if not df.empty:
            last_processed_times[name] = df.iloc[-1]['time']
    print(f"时间对齐完成，正在监控: {list(MONITOR_TFS.keys())}")
    print("-" * 50)

    try:
        while True:
            manage_trailing_logic()

            rsi_report = []
            for name, tf_code in MONITOR_TFS.items():
                df = get_processed_data(tf_code)
                if df.empty: continue

                curr_bar = df.iloc[-1]
                rsi_report.append(f"{name}:{curr_bar['rsi']:.1f}")

                # 只有当K线更新时才检测交叉
                if curr_bar['time'] != last_processed_times[name]:
                    prev_bar = df.iloc[-2]
                    order_type = None
                    if prev_bar['macd'] <= prev_bar['signal'] and curr_bar['macd'] > curr_bar['signal']:
                        order_type = mt5.ORDER_TYPE_BUY
                    elif prev_bar['macd'] >= prev_bar['signal'] and curr_bar['macd'] < curr_bar['signal']:
                        order_type = mt5.ORDER_TYPE_SELL

                    if order_type is not None:
                        mode = get_signal_type(df, order_type)
                        if mode: execute_trade(order_type, name, mode)

                    last_processed_times[name] = curr_bar['time'] # 更新时间戳

            # 实时状态栏
            tick = mt5.symbol_info_tick(SYMBOL)
            all_pos = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
            total_p = sum([p.profit for p in all_pos]) if all_pos else 0.0
            print(f"\r[时间:{datetime.now().strftime('%H:%M:%S')} | 现价:{tick.bid:<8.2f}] | 仓位:{len(all_pos)} | 盈亏:${total_p:+.2f} ", end="", flush=True)

            time.sleep(1)

    except KeyboardInterrupt:
        print("\n用户停止程序")
    finally:
        mt5.shutdown()

if __name__ == "__main__":
    run_loop()