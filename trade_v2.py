import MetaTrader5 as mt5
import pandas as pd
import pandas_ta as ta
import time
import smtplib
import os
from dotenv import load_dotenv
from email.mime.text import MIMEText
from datetime import datetime

# ================= 配置区 =================
load_dotenv()

SYMBOL = os.getenv("SYMBOL", "GOLD_")
MAGIC_NUMBER = int(os.getenv("MAGIC_NUMBER", 20260430))
VOLUME = float(os.getenv("VOLUME", 0.01))
SL_USD = float(os.getenv("SL_USD", 12.0))      # 建议拉大至12，抗震荡
TP_USD = float(os.getenv("TP_USD", 18.0))
BE_PROFIT = float(os.getenv("BE_PROFIT", 3.0))
TRAIL_STEP = float(os.getenv("TRAIL_STEP", 3.0))
MAX_TOTAL_POSITIONS = 1                       # 强制单一持仓，防止互博

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
    "1H": mt5.TIMEFRAME_H1
    # 4H 作为趋势判断，不直接作为下单周期
}

# ================= 核心功能函数 =================

def send_email_heartbeat(content):
    try:
        msg = MIMEText(content)
        msg['From'] = SENDER_EMAIL
        msg['To'] = RECEIVER_EMAIL
        msg['Subject'] = "交易机器人心跳监控"
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.send_message(msg)
        print(f"\n  [MAIL] 邮件心跳发送成功")
    except Exception as e:
        print(f"\n[!] 邮件发送失败: {e}")

def send_email_notification(subject, content):
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
        print(f"  [MAIL ERROR] 邮件发送失败: {e}")

def get_processed_data(tf):
    rates = mt5.copy_rates_from_pos(SYMBOL, tf, 0, 150)
    if rates is None or len(rates) < 50: return pd.DataFrame()
    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.rsi(length=14, append=True)
    df.rename(columns={'MACD_12_26_9': 'macd', 'MACDs_12_26_9': 'signal', 'MACDh_12_26_9': 'hist', 'RSI_14': 'rsi'}, inplace=True)
    return df

def get_signal_type(tf_name, df, df_4h, order_type):
    """
    升级版逻辑：引入 4H 趋势过滤，拒绝逆势抄底
    """
    if df.empty or df_4h.empty: return None
    curr, prev = df.iloc[-1], df.iloc[-2]
    curr_4h = df_4h.iloc[-1]
    
    # 4H 趋势方向判断 (MACD柱状图或快慢线方向)
    is_4h_bullish = curr_4h['macd'] > curr_4h['signal']
    stat_msg = f"RSI:{curr['rsi']:.1f} | 4H趋势:{'多' if is_4h_bullish else '空'}"

    # --- 买入逻辑 ---
    if order_type == mt5.ORDER_TYPE_BUY:
        # 过滤：如果 4H 是空头，除非极度超卖(RSI<30)，否则严禁做多
        if not is_4h_bullish and curr_4h['rsi'] > 30: return None
        # 顺势或回调买入
        if curr['hist'] > prev['hist'] and curr['rsi'] < 65:
            print(f"  [√] {tf_name} 匹配买入: {stat_msg}")
            return "BUY_MODE"

    # --- 卖出逻辑 ---
    elif order_type == mt5.ORDER_TYPE_SELL:
        # 过滤：如果 4H 是多头，除非极度超买(RSI>70)，否则严禁做空
        if is_4h_bullish and curr_4h['rsi'] < 70: return None
        # 顺势或回调卖出
        if curr['hist'] < prev['hist'] and curr['rsi'] > 35:
            print(f"  [√] {tf_name} 匹配卖出: {stat_msg}")
            return "SELL_MODE"
    return None

def execute_trade(order_type, tf_name, signal_mode):
    # 强制执行单仓位，彻底解决互博导致的亏损
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    if positions and len(positions) >= MAX_TOTAL_POSITIONS:
        return

    tick = mt5.symbol_info_tick(SYMBOL)
    info = mt5.symbol_info(SYMBOL)
    price = tick.ask if order_type == 0 else tick.bid
    sl = round(price - SL_USD, info.digits) if order_type == 0 else round(price + SL_USD, info.digits)
    tp = round(price + TP_USD, info.digits) if order_type == 0 else round(price - TP_USD, info.digits)

    request = {
        "action": mt5.TRADE_ACTION_DEAL, "symbol": SYMBOL, "volume": VOLUME,
        "type": order_type, "price": price, "sl": sl, "tp": tp,
        "magic": MAGIC_NUMBER, "comment": f"{signal_mode}:{tf_name}",
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    res = mt5.order_send(request)
    if res.retcode == mt5.TRADE_RETCODE_DONE:
        side = 'BUY' if order_type == 0 else 'SELL'
        print(f"\n✅ 成交: #{res.order} | {side} | {tf_name}")
        send_email_notification(f"成交通知: {side} {SYMBOL}", f"周期:{tf_name}\n价格:{price}")

# ================= 主循环 =================
def run_loop():
    if not mt5.initialize(): return
    last_processed_times = {name: None for name in MONITOR_TFS}
    last_heartbeat = datetime.now()

    try:
        while True:
            # 每一轮都获取最新的 4H 趋势数据作为过滤基准
            df_4h = get_processed_data(mt5.TIMEFRAME_H4)
            
            # 心跳
            if (datetime.now() - last_heartbeat).total_seconds() >= 1800:
                send_email_heartbeat(f"系统运行正常 | 时间: {datetime.now()}")
                last_heartbeat = datetime.now()

            for name, tf_code in MONITOR_TFS.items():
                df = get_processed_data(tf_code)
                if df.empty or df.iloc[-1]['time'] == last_processed_times[name]: continue
                
                curr, prev = df.iloc[-1], df.iloc[-2]
                order_type = None
                if prev['macd'] <= prev['signal'] and curr['macd'] > curr['signal']: order_type = mt5.ORDER_TYPE_BUY
                elif prev['macd'] >= prev['signal'] and curr['macd'] < curr['signal']: order_type = mt5.ORDER_TYPE_SELL
                
                if order_type is not None:
                    # 传入 df_4h 进行强过滤
                    mode = get_signal_type(name, df, df_4h, order_type)
                    if mode: execute_trade(order_type, name, mode)
                
                last_processed_times[name] = df.iloc[-1]['time']
            time.sleep(1)
    finally:
        mt5.shutdown()

if __name__ == "__main__":
    run_loop()