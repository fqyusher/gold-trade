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

def send_email_heartbeat(content):
    """发送邮件提醒"""
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
        # msg.attach(MIMEText(content, 'plain'))
        # server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        # server.starttls()
        # server.login(EMAIL_USER, EMAIL_PASS)
        # server.sendmail(EMAIL_USER, EMAIL_TO, msg.as_string())
        # server.quit()
        # print(f"\n[♥ 邮件心跳发送成功] {datetime.now().strftime('%H:%M:%S')}")
    except Exception as e:
        print(f"\n[!] 邮件发送失败: {e}")

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
        print(f"  [MAIL] 通知已发送至 {RECEIVER_EMAIL}")
    except Exception as e:
        print(f"  [MAIL ERROR] 邮件发送失败: {e}")

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

def get_signal_type(tf_name, df, order_type):
    """判断信号模式并记录详细分析日志"""
    if df.empty or len(df) < 20: return None
    curr, prev = df.iloc[-1], df.iloc[-2]
    rsi_hist = df['rsi'].tail(8)

    stat_msg = f"RSI:{curr['rsi']:.1f} | Hist:{curr['hist']:.4f} | MACD:{curr['macd']:.4f}"

    if order_type == mt5.ORDER_TYPE_BUY:
        # 反转模式
        if rsi_hist.min() <= 35:
            if curr['hist'] > prev['hist'] and curr['rsi'] < 60:
                print(f"  [√] {tf_name} 匹配反转买入: {stat_msg}")
                return "REVERSAL"
            else:
                print(f"  [×] {tf_name} 满足超跌但动能未加速或RSI过高: {stat_msg}")
        # 顺势模式
        if curr['macd'] > 0 and curr['rsi'] > 52:
            if curr['hist'] > prev['hist']:
                print(f"  [√] {tf_name} 匹配顺势买入: {stat_msg}")
                return "TREND"
            else:
                print(f"  [×] {tf_name} 满足强势区间但动能减弱: {stat_msg}")

    elif order_type == mt5.ORDER_TYPE_SELL:
        # 反转模式
        if rsi_hist.max() >= 65:
            if curr['hist'] < prev['hist'] and curr['rsi'] > 40:
                print(f"  [√] {tf_name} 匹配反转卖出: {stat_msg}")
                return "REVERSAL"
            else:
                print(f"  [×] {tf_name} 满足超买但动能未加速: {stat_msg}")
        # 顺势模式
        if curr['macd'] < 0 and curr['rsi'] < 48:
            if curr['hist'] < prev['hist']:
                print(f"  [√] {tf_name} 匹配顺势卖出: {stat_msg}")
                return "TREND"
            else:
                print(f"  [×] {tf_name} 满足弱势区间但动能回升: {stat_msg}")

    return None

def manage_trailing_logic():
    """实时追踪止损维护并输出变动日志"""
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    if not positions: return
    for pos in positions:
        entry, curr, sl = pos.price_open, pos.price_current, pos.sl
        profit_usd = (curr - entry) * 100 * pos.volume if pos.type == 0 else (entry - curr) * 100 * pos.volume

        new_sl = 0
        log_type = ""

        if pos.type == 0: # 多单
            if profit_usd >= BE_PROFIT and (sl < entry or sl == 0):
                new_sl = entry + 0.05
                log_type = "保本触发"
            elif profit_usd >= TRAIL_STEP * 2:
                target = entry + (int(profit_usd // TRAIL_STEP) - 1) * (TRAIL_STEP / (pos.volume * 100))
                if target > sl + 0.1:
                    new_sl = target
                    log_type = "追踪上移"
        else: # 空单
            if profit_usd >= BE_PROFIT and (sl > entry or sl == 0):
                new_sl = entry - 0.05
                log_type = "保本触发"
            elif profit_usd >= TRAIL_STEP * 2:
                target = entry - (int(profit_usd // TRAIL_STEP) - 1) * (TRAIL_STEP / (pos.volume * 100))
                if target < sl - 0.1:
                    new_sl = target
                    log_type = "追踪下移"

        if new_sl > 0:
            res = mt5.order_send({"action": mt5.TRADE_ACTION_SLTP, "position": pos.ticket, "sl": round(new_sl, 2), "tp": pos.tp})
            if res.retcode == mt5.TRADE_RETCODE_DONE:
                print(f"\n  [TS] {log_type}: 订单#{pos.ticket} 止损调整至 {round(new_sl, 2)} (当前利润:${profit_usd:.2f})")

def execute_trade(order_type, tf_name, signal_mode):
    """执行下单任务并记录结果"""
    positions = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
    current_comment = f"{signal_mode}:{tf_name}"

    # 唯一性与上限检查
    if positions:
        if len(positions) >= MAX_TOTAL_POSITIONS:
            print(f"  [!] 拦截: 仓位已满 ({len(positions)})")
            return
        for pos in positions:
            if pos.comment == current_comment:
                return

    tick = mt5.symbol_info_tick(SYMBOL)
    info = mt5.symbol_info(SYMBOL)
    if not tick or not info: return

    price = tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid
    sl = round(price - SL_USD, info.digits) if order_type == 0 else round(price + SL_USD, info.digits)
    tp = round(price + TP_USD, info.digits) if order_type == 0 else round(price - TP_USD, info.digits)

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
    }

    print(f"  [>] 正在提交订单: {current_comment}...")
    res = mt5.order_send(request)
    if res.retcode == mt5.TRADE_RETCODE_DONE:
        side = 'BUY' if order_type == 0 else 'SELL'
        print(f"\n✅ 交易成交: #{res.order} | 方向:{side} | 价格:{price} | 模式:{current_comment}")
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
        print(f"  [ERROR] 下单失败: {res.comment} (代码:{res.retcode})")

# ================= 主循环 =================

def run_loop():
    if not mt5.initialize():
        print("CRITICAL: MT5 初始化失败")
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
            # 心跳检测 (30分钟)
            if (datetime.now() - last_heartbeat).total_seconds() >= 1800:
                pos_info = len(mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER) or [])
                send_email_heartbeat(f"系统运行正常\n当前持仓: {pos_info}\n运行时间: {datetime.now()}")
                last_heartbeat = datetime.now()

            rsi_report = []
            for name, tf_code in MONITOR_TFS.items():
                df = get_processed_data(tf_code)
                if df.empty: continue

                curr_bar = df.iloc[-1]
                rsi_report.append(f"{name}:{curr_bar['rsi']:.1f}")

                # 仅在K线更新时检测交叉
                if curr_bar['time'] != last_processed_times[name]:
                    prev_bar = df.iloc[-2]
                    order_type = None

                    if prev_bar['macd'] <= prev_bar['signal'] and curr_bar['macd'] > curr_bar['signal']:
                        order_type = mt5.ORDER_TYPE_BUY
                        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔔 {name} 周期触发金叉...")
                    elif prev_bar['macd'] >= prev_bar['signal'] and curr_bar['macd'] < curr_bar['signal']:
                        order_type = mt5.ORDER_TYPE_SELL
                        print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 🔔 {name} 周期触发死叉...")

                    if order_type is not None:
                        mode = get_signal_type(name, df, order_type)
                        if mode:
                            execute_trade(order_type, name, mode)
                        else:
                            print(f"  [!] {name} 信号未通过二次过滤")

                    last_processed_times[name] = curr_bar['time']

            # 底部实时状态刷新
            all_pos = mt5.positions_get(symbol=SYMBOL, magic=MAGIC_NUMBER)
            total_p = sum([p.profit for p in all_pos]) if all_pos else 0.0
            tick = mt5.symbol_info_tick(SYMBOL)
            cur_p = tick.bid if tick else 0.0

            print(f"\r[{datetime.now().strftime('%H:%M:%S')} | 现价:{cur_p:<8.2f}] | [{' | '.join(rsi_report)}] | 持仓:{len(all_pos)} | 盈亏:${total_p:+.2f} ", end="", flush=True)

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