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

SYMBOL = os.getenv("SYMBOL", "GOLD_")
MAGIC_NUMBER = 20260506 #int(os.getenv("MAGIC_NUMBER", 20260430))
VOLUME = float(os.getenv("VOLUME", 0.01))
# 黄金波动大，建议环境变量中 SL_USD=10.0, TP_USD=20.0
SL_USD = float(os.getenv("SL_USD", 10.0))
TP_USD = float(os.getenv("TP_USD", 20.0))
# BE_PROFIT 建议设为 3.0 或 4.0 以上，避免频繁被打保本
BE_PROFIT = float(os.getenv("BE_PROFIT", 3.0)) 
TRAIL_STEP = float(os.getenv("TRAIL_STEP", 2.0))
MAX_TOTAL_POSITIONS = int(os.getenv("MAX_POSITIONS", 5))
MAX_DIRECTION_POS = int(os.getenv("MAX_DIRECTION_POS", 3))

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
        print(f"  [MAIL] 邮件心跳发送成功 {RECEIVER_EMAIL}")
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
        print(f"  [MAIL] 通知已发送至 {RECEIVER_EMAIL}")
    except Exception as e:
        print(f"  [MAIL ERROR] 邮件发送失败: {e}")

def get_processed_data(tf):
    rates = mt5.copy_rates_from_pos(SYMBOL, tf, 0, 250)
    if rates is None or len(rates) < 200: return pd.DataFrame()

    df = pd.DataFrame(rates)
    df['time'] = pd.to_datetime(df['time'], unit='s')
    df['ema200'] = ta.ema(df['close'], length=200)
    df['atr'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    df.ta.macd(fast=12, slow=26, signal=9, append=True)
    df.ta.rsi(length=14, append=True)

    df.rename(columns={
        'MACD_12_26_9': 'macd', 'MACDs_12_26_9': 'signal',
        'MACDh_12_26_9': 'hist', 'RSI_14': 'rsi'
    }, inplace=True)
    return df

def get_signal_type(tf_name, df, df_h1, order_type):
    if df.empty or len(df) < 30 or df_h1.empty: return None
    
    # 🚨 修正：严格使用上一根【已收盘】的 K 线进行指标判断，拒绝未来函数
    curr_closed = df.iloc[-2]
    prev_closed = df.iloc[-3]
    
    # 趋势基准同样使用上一根已收盘的 H1 判定，防止 H1 当下正在刺穿 EMA
    h1_closed = df_h1.iloc[-2]
    is_bull_trend = h1_closed['close'] > h1_closed['ema200']
    
    # ATR 波动率过滤 (检测极端行情)
    avg_atr = df['atr'].tail(20).mean()
    is_high_vol = curr_closed['atr'] > (avg_atr * 1.5)
    
    # --- 做多逻辑 ---
    if order_type == mt5.ORDER_TYPE_BUY:
        # 顺势：H1看多 + RSI未超买(空间足够) + 动能放大
        if is_bull_trend and 45 < curr_closed['rsi'] < 65 and curr_closed['hist'] > prev_closed['hist']:
            return "TREND"
        # 反转：H1看空 + 极端暴跌 + 极度超卖 + MACD柱体开始缩短拐头 (避免死接飞刀)
        if not is_bull_trend and is_high_vol and curr_closed['rsi'] < 30 and curr_closed['hist'] > prev_closed['hist']:
            return "REVERSAL"
            
    # --- 做空逻辑 ---
    elif order_type == mt5.ORDER_TYPE_SELL:
        # 顺势：H1看空 + RSI未超卖(空间足够) + 动能放大
        if not is_bull_trend and 30 < curr_closed['rsi'] < 60 and curr_closed['hist'] < prev_closed['hist']:
            return "TREND"
        # 反转：H1看多 + 极端暴涨 + 极度超买 + MACD柱体开始缩短拐头
        if is_bull_trend and is_high_vol and curr_closed['rsi'] > 70 and curr_closed['hist'] < prev_closed['hist']:
            return "REVERSAL"
            
    return None

def manage_trailing_logic():
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    if not positions: return
    for pos in positions:
        entry, curr, sl = pos.price_open, pos.price_current, pos.sl
        profit_usd = (curr - entry) * 100 * pos.volume if pos.type == 0 else (entry - curr) * 100 * pos.volume
        
        new_sl = 0
        # 🚨 修正：黄金点差保护，缓冲垫扩大至 0.30
        buffer = 0.30 
        
        if pos.type == 0: # 多单
            if profit_usd >= BE_PROFIT and (sl < entry or sl == 0): 
                new_sl = entry + buffer
            elif profit_usd >= TRAIL_STEP * 2:
                target = entry + (int(profit_usd // TRAIL_STEP) - 1) * (TRAIL_STEP / (pos.volume * 100))
                if target > sl + 0.1: new_sl = target
        else: # 空单
            if profit_usd >= BE_PROFIT and (sl > entry or sl == 0): 
                new_sl = entry - buffer
            elif profit_usd >= TRAIL_STEP * 2:
                target = entry - (int(profit_usd // TRAIL_STEP) - 1) * (TRAIL_STEP / (pos.volume * 100))
                if target < sl - 0.1: new_sl = target
                
        if new_sl > 0:
            mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "sl": round(new_sl, 2), "tp": pos.tp})

def execute_trade(order_type, tf_name, signal_mode):
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    current_comment = f"{signal_mode}:{tf_name}"

    if positions:
        if len(positions) >= MAX_TOTAL_POSITIONS:
            return

        buy_count = sum(1 for p in positions if p.type == 0)
        sell_count = sum(1 for p in positions if p.type == 1)

        if order_type == 0 and buy_count >= MAX_DIRECTION_POS:
            return
        if order_type == 1 and sell_count >= MAX_DIRECTION_POS:
            return
        # if len(positions) >= MAX_TOTAL_POSITIONS: return
        # 防止同一周期同方向重复开单
        for pos in positions:
            if pos.comment == current_comment: return

    tick = mt5.symbol_info_tick(SYMBOL)
    info = mt5.symbol_info(SYMBOL)
    if not tick or not info: return

    price = tick.ask if order_type == 0 else tick.bid
    sl = round(price - SL_USD if order_type == 0 else price + SL_USD, info.digits)
    tp = round(price + TP_USD if order_type == 0 else price - TP_USD, info.digits)

    request = {
        "action": mt5.TRADE_ACTION_DEAL, 
        "symbol": SYMBOL, 
        "volume": VOLUME,
        "type": order_type, 
        "price": price, 
        "sl": sl, 
        "tp": tp,
        "magic": MAGIC_NUMBER, 
        "comment": current_comment,
        "type_filling": mt5.ORDER_FILLING_IOC,
        "deviation": 20 # 🚨 补充：防止极端行情下滑点过大被拒单
    }

    res = mt5.order_send(request)
    if res.retcode == mt5.TRADE_RETCODE_DONE:
        print(f"✅ 成交: {current_comment} @ {price}")
        side = 'BUY' if order_type == 0 else 'SELL'
        # 实时拉取最新仓位情况（安全避免未定义变量）
        all_pos = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
        total_pnl = sum([p.profit for p in all_pos]) if all_pos else 0.0
        email_content = f"""
        【交易详情】
        ---------------------------
        成交时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
        交易品种: {SYMBOL}
        交易方向: {side}
        监控周期: {tf_name}
        成交价格: {price}
        下单模式: {signal_mode}
        
        【风控参数】
        止损 (SL): {sl}
        止盈 (TP): {tp}
        交易量: {VOLUME}
        
        【账户概况】
        当前持仓数: {len(all_pos)}
        当前累计浮盈: {total_pnl:.2f}
        ---------------------------
        """
        send_email_notification(f"【交易提醒】: {side}-{tf_name}周期", email_content)
    else:
        print(f"❌ 订单被拒: {res.comment}")

def run_loop():
    if not mt5.initialize(): 
        print("MT5 初始化失败")
        return
        
    # send_email_notification("test", "test")
    print("=" * 60)
    print(f"🤖 系统启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"监控品种: {SYMBOL} | 止损/止盈: ${SL_USD}/${TP_USD}")

    # --- 时间对齐：防止重启时把历史交叉误判为新信号 ---
    last_processed_times = {}
    for name, tf_code in MONITOR_TFS.items():
        df = get_processed_data(tf_code)
        if not df.empty:
            last_processed_times[name] = df.iloc[-1]['time']
            print(f"  [OK] {name} 周期时间已对齐: {last_processed_times[name]}")
    print("=" * 60)
    last_heartbeat = datetime.now()
    # send_email_heartbeat(f"x系统监控开始")
    # execute_trade(mt5.ORDER_TYPE_BUY, "test", "test")
    
    try:
        while True:
            manage_trailing_logic()
            
            df_h1 = get_processed_data(mt5.TIMEFRAME_H1)
            
            if (datetime.now() - last_heartbeat).total_seconds() >= 1800:
                total_p = sum([p.profit for p in (mt5.positions_get(magic=MAGIC_NUMBER) or [])])
                send_email_heartbeat(f"系统运行正常, 累计浮盈: {total_p:.2f} USD")
                last_heartbeat = datetime.now()

            for name, tf_code in MONITOR_TFS.items():
                df = get_processed_data(tf_code)
                if df.empty or len(df) < 5: continue
                
                # 🚨 判定新 K 线是否生成 (核心修复区)
                # 通过监控正在运行的 K 线时间戳变化，来确认上一根 K 线是否已彻底收盘
                active_bar_time = df.iloc[-1]['time']
                
                if active_bar_time != last_processed_times.get(name):
                    # 只有在产生新 K 线的那一瞬间，才去判断前两根已收盘的交叉状态
                    curr_closed = df.iloc[-2]
                    prev_closed = df.iloc[-3]
                    
                    order_type = None
                    if prev_closed['macd'] <= prev_closed['signal'] and curr_closed['macd'] > curr_closed['signal']:
                        order_type = mt5.ORDER_TYPE_BUY
                    elif prev_closed['macd'] >= prev_closed['signal'] and curr_closed['macd'] < curr_closed['signal']:
                        order_type = mt5.ORDER_TYPE_SELL
                    
                    if order_type is not None:
                        mode = get_signal_type(name, df, df_h1, order_type)
                        if mode: 
                            execute_trade(order_type, name, mode)
                            
                    # 更新记录的时间戳
                    last_processed_times[name] = active_bar_time

            # 底部实时状态刷新
            all_pos = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
            total_p = sum([p.profit for p in all_pos]) if all_pos else 0.0
            tick = mt5.symbol_info_tick(SYMBOL)
            cur_p = tick.bid if tick else 0.0

            print(f"\r[{datetime.now().strftime('%H:%M:%S')} | 现价:{cur_p:<8.2f}] | 持仓:{len(all_pos)} | 盈亏:${total_p:+.2f} ", end="", flush=True)

                    
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\n[INFO] 用户手动停止程序。")
    except Exception as e:
        print(f"\n\n[CRASH] 程序崩溃: {e}")
        send_email_notification("MT5 系统崩溃报警", f"异常详情: {e}")
    finally:
        mt5.shutdown()
        print("[INFO] MT5 连接已安全断开。")

if __name__ == "__main__":
    run_loop()